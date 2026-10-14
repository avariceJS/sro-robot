import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";

const require = createRequire(import.meta.url);
const [baselinePath, patchedPath] = process.argv.slice(2);
assert.ok(baselinePath && patchedPath, "usage: node compare_upload_retry_revisions.mjs <baseline-module> <patched-module>");
const baseline = require(baselinePath);
const patched = require(patchedPath);
const cryptoApi = globalThis.crypto;
assert.ok(cryptoApi?.subtle, "WebCrypto is required");

function memoryStorage() {
  const values = new Map();
  return {
    values,
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  };
}

function file(name, content) {
  const bytes = new TextEncoder().encode(content);
  return {
    name,
    size: bytes.byteLength,
    lastModified: 1000,
    arrayBuffer: async () => bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength),
  };
}

const keySlot = "nightly-differential";
const initialFiles = [file("accepted.pdf", "accepted page"), file("rejected.pdf", "invalid page")];
const correctedFiles = [initialFiles[0], file("rejected.pdf", "corrected page")];
const firstPageKey = "a".repeat(48);
const reopenedPageKey = "b".repeat(48);

// Baseline behavior: a definite 4xx clears the whole batch key. A new page token
// therefore derives a different key for the already accepted first chunk.
const baselineStorage = memoryStorage();
const initialBase = await baseline.resolveBatchKey(initialFiles, firstPageKey, {
  cryptoApi,
  storage: baselineStorage,
  storageKey: keySlot,
});
const baselineAcceptedChunkKey = await baseline.chunkIdempotencyKey(initialBase, 0, cryptoApi);
baseline.clearBatchKey({ storage: baselineStorage, storageKey: keySlot }, initialBase);
const retryBase = await baseline.resolveBatchKey(correctedFiles, reopenedPageKey, {
  cryptoApi,
  storage: baselineStorage,
  storageKey: keySlot,
});
const baselineRetryAcceptedChunkKey = await baseline.chunkIdempotencyKey(retryBase, 0, cryptoApi);
const baselineDuplicateAcceptedChunk = baselineRetryAcceptedChunkKey !== baselineAcceptedChunkKey;

// Patched behavior: keep the accepted chunk key and clear only the rejected one.
const patchedStorage = memoryStorage();
const initialChunks = await patched.resolveBatchKeys(initialFiles, firstPageKey, 1, {
  cryptoApi,
  storage: patchedStorage,
  storageKey: keySlot,
});
patched.clearChunkKeys(
  { storage: patchedStorage, storageKey: keySlot },
  [initialChunks.chunkFingerprints[1]],
  [initialChunks.keys[1]],
);
const retriedChunks = await patched.resolveBatchKeys(correctedFiles, reopenedPageKey, 1, {
  cryptoApi,
  storage: patchedStorage,
  storageKey: keySlot,
});
const patchedDuplicateAcceptedChunk = retriedChunks.keys[0] !== initialChunks.keys[0];

const result = {
  evidence: "local client-key differential; no network or external service",
  baselineModule: pathToFileURL(baselinePath).href,
  patchedModule: pathToFileURL(patchedPath).href,
  scenario: "chunk 1 accepted; chunk 2 returns HTTP 422; edit only chunk 2; reopen page with a fresh generated key",
  baseline: {
    acceptedChunkKeyRetained: !baselineDuplicateAcceptedChunk,
    acceptedChunkKeyChangedOnRetry: baselineDuplicateAcceptedChunk,
    expectedResult: "duplicate-risk reproduced",
  },
  patched: {
    acceptedChunkKeyRetained: !patchedDuplicateAcceptedChunk,
    acceptedChunkKeyChangedOnRetry: patchedDuplicateAcceptedChunk,
    rejectedChunkKeyChanged: retriedChunks.keys[1] !== initialChunks.keys[1],
    expectedResult: "accepted chunk remains idempotent; corrected chunk gets a new key",
  },
};

console.log("PPV_REACHED");
assert.equal(baselineDuplicateAcceptedChunk, true, "baseline must reproduce the duplicate risk");
assert.equal(patchedDuplicateAcceptedChunk, false, "patch must retain the accepted chunk key");
assert.notEqual(retriedChunks.keys[1], initialChunks.keys[1], "corrected rejected chunk needs a fresh key");
console.log(JSON.stringify(result, null, 2));
