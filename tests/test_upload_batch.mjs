import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";

const require = createRequire(import.meta.url);
const { chunkFiles, validateFiles, chunkIdempotencyKey, filesFingerprint, resolveBatchKey, clearBatchKey, uploadChunks } = require(
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

test("same selected files retain their batch key across reload and changed files get a fresh key", async () => {
  const storage = new Map();
  const storageApi = {
    getItem: (key) => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, value),
    removeItem: (key) => storage.delete(key),
  };
  const options = { cryptoApi: webCrypto(), storage: storageApi, storageKey: "test-batch" };
  const firstFiles = [fingerprintable("one.pdf", "first"), fingerprintable("two.pdf", "second")];
  const first = await resolveBatchKey(firstFiles, "a".repeat(48), options);
  const afterReload = await resolveBatchKey(firstFiles, "b".repeat(48), options);
  assert.equal(afterReload, first);

  const changed = await resolveBatchKey(
    [fingerprintable("one.pdf", "changed"), firstFiles[1]],
    "c".repeat(48),
    options,
  );
  assert.equal(changed, "c".repeat(48));
  clearBatchKey(options, changed);
  assert.equal(storage.has("test-batch"), false);
  const nextBatch = await resolveBatchKey(firstFiles, "d".repeat(48), options);
  assert.equal(nextBatch, "d".repeat(48));
  assert.notEqual(await filesFingerprint(firstFiles, webCrypto()), "");
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
