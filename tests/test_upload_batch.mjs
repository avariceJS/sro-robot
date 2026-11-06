import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";

const require = createRequire(import.meta.url);
const { chunkFiles, validateFiles, chunkIdempotencyKey, filesFingerprint, resolveBatchKeys, rememberOperation, clearChunkKeys, uploadChunks, transportManifest, uploadTransportChunks, dispatchMixedUploadAccepted } = require(
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

test("100 percent transmitted bytes are not accepted until the server receipt", async () => {
  const batch = {id: "d".repeat(32), file_count: 1, files_per_chunk: 2, chunks: [{index: 0, accepted: false}]};
  const progress = [], accepted = [];
  const xhr = {upload: {}, open(_method, url) { this.responseURL = url; }, setRequestHeader() {}, send() {}};
  const pending = uploadTransportChunks({
    endpoint: "http://127.0.0.1/upload", batch, files: corpus(1),
    formDataFactory: () => new FakeFormData(), xhrFactory: () => xhr,
    onUploadProgress: (event) => progress.push(event), onProgress: (count) => accepted.push(count),
  });
  xhr.upload.onprogress({loaded: 1200, total: 1200, lengthComputable: true});
  assert.equal(progress.at(-1).loaded, 1200);
  assert.equal(progress.at(-1).acceptedFiles, 0);
  assert.deepEqual(accepted, []);
  assert.equal(batch.chunks[0].accepted, false);
  xhr.status = 200;
  xhr.responseText = JSON.stringify({batch_id: batch.id, chunk_index: 0, operation_id: 11, task_ids: [21]});
  xhr.onload();
  assert.equal((await pending).complete, true);
  assert.deepEqual(accepted, [1]);
});

test("XHR interruption preserves an unconfirmed chunk and never resends", async () => {
  const batch = {id: "e".repeat(32), file_count: 1, files_per_chunk: 2, chunks: [{index: 0, accepted: false}]};
  let sent = 0;
  const xhr = {upload: {}, open() {}, setRequestHeader() {}, send() { sent += 1; }};
  const pending = uploadTransportChunks({endpoint: "http://127.0.0.1/upload", batch, files: corpus(1),
    formDataFactory: () => new FakeFormData(), xhrFactory: () => xhr});
  xhr.onerror();
  const result = await pending;
  assert.equal(result.unknownOutcome, true);
  assert.equal(result.acceptedFiles, 0);
  assert.equal(batch.chunks[0].accepted, false);
  assert.equal(sent, 1);
});

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

test("preflight accepts HEIC and rejects empty, unsupported, oversized and excessive selections", () => {
  const limits = { maxFiles: 100, maxFileBytes: 1000, maxBatchBytes: 5000 };
  assert.match(validateFiles([], limits), /Выберите/);
  assert.match(validateFiles(corpus(101), limits), /не более 100/);
  assert.equal(validateFiles([{ name: "photo.HEIC", size: 10 }], limits), null);
  assert.match(validateFiles([{ name: "photo.gif", size: 10 }], limits), /Поддерживаются/);
  assert.equal(validateFiles([{ name: "photo.WEBP", size: 10 }], limits), null);
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

test("a changed earlier chunk avoids reusing its old key in the same page", async () => {
  const storage = memoryStorage();
  const options = { cryptoApi: webCrypto(), storage, storageKey: "same-page-edit" };
  const files = [fingerprintable("accepted.pdf", "accepted first chunk"),
    fingerprintable("rejected.pdf", "rejected second chunk")];
  const initial = await resolveBatchKeys(files, "a".repeat(48), 1, options);
  rememberOperation(options, initial.chunkFingerprints[0], 81);
  clearChunkKeys(options, [initial.chunkFingerprints[1]], [initial.keys[1]]);

  const edited = await resolveBatchKeys(
    [fingerprintable("corrected.pdf", "changed first chunk"), files[1]],
    "a".repeat(48), 1, options,
  );

  assert.notEqual(edited.keys[0], initial.keys[0]);
  assert.equal(edited.keys[1], initial.keys[1]);
  assert.deepEqual(edited.operationIds, [null, null]);
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
      operations: {},
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

test("a failed persistent-storage write stops before deriving upload keys", async () => {
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

  await assert.rejects(resolveBatchKeys(files, "7".repeat(48), 1, {
    cryptoApi: webCrypto(),
    storage: unavailableStorage,
    legacyStorage: session,
    storageKey,
  }), /ключ безопасного повтора/);
  assert.notEqual(session.getItem(storageKey), null);
});

test("a storage write that cannot be read back fails closed", async () => {
  const storage = memoryStorage();
  const files = [fingerprintable("pending.pdf", "unconfirmed")];
  const mismatchedReadback = {
    getItem: (key) => storage.getItem(key) === null ? null : "stale value",
    setItem: storage.setItem,
  };
  await assert.rejects(resolveBatchKeys(files, "8".repeat(48), 1, {
    cryptoApi: webCrypto(), storage: mismatchedReadback, storageKey: "sro-upload-batch-v1",
  }), /ключ безопасного повтора/);
});

test("the upload form continues safely when browser storage is disabled", async () => {
  const files = [fingerprintable("pending.pdf", "unconfirmed")];
  let submitHandler;
  let fetchCalls = 0;
  const fileInput = { files, disabled: false };
  const button = { disabled: false, textContent: "Загрузить" };
  const idempotencyInput = { value: "9".repeat(48) };
  const form = {
    action: "http://127.0.0.1/upload",
    dataset: { maxFiles: "100", maxFileBytes: "1000", maxBatchBytes: "1000", filesPerRequest: "20" },
    querySelector(selector) {
      if (selector === 'input[type="file"]') return fileInput;
      if (selector === 'button[type="submit"]') return button;
      if (selector === 'input[name="idempotency_key"]') return idempotencyInput;
      return null;
    },
    addEventListener(type, handler) { if (type === "submit") submitHandler = handler; },
  };
  const status = { textContent: "" };
  const document = { addEventListener() {}, querySelector: (selector) => selector === "form#document-upload-form" ? form : status };
  const root = {
    addEventListener() {},
    crypto: webCrypto(),
    TextEncoder,
    setTimeout,
    fetch: async () => { fetchCalls += 1; throw new Error("must not send"); },
    location: { assign() {} },
  };
  Object.defineProperty(root, "localStorage", {
    get() { throw new Error("storage disabled"); },
  });
  root.document = document;
  runInNewContext(readFileSync(new URL("../app/web/static/upload-batch.js", import.meta.url), "utf8"), {
    globalThis: root,
    document,
    module: undefined,
    TextEncoder,
    URL,
    FormData: FakeFormData,
  });
  assert.equal(typeof submitHandler, "function");
  await submitHandler({ preventDefault() {} });
  assert.equal(fetchCalls, 1);
  assert.match(status.textContent, /не закрывайте вкладку/);
});

test("session fallback retains retry keys after persistent storage quota failure", async () => {
  const local = { getItem: () => null, setItem: () => { throw new Error("quota"); } };
  const session = memoryStorage();
  const options = {
    cryptoApi: webCrypto(), storage: local, fallbackStorage: session, legacyStorage: session,
    storageKey: "quota-fallback",
  };
  const files = [fingerprintable("repeat.jpg", "same image bytes")];
  const first = await resolveBatchKeys(files, "a".repeat(48), 20, options);
  assert.equal(first.storage, session);
  rememberOperation({ storage: first.storage, storageKey: options.storageKey }, first.chunkFingerprints[0], 51);
  const afterReload = await resolveBatchKeys(files, "b".repeat(48), 20, options);
  assert.deepEqual(afterReload.keys, first.keys);
  assert.deepEqual(afterReload.operationIds, [51]);
});

test("a legacy pending batch in session storage is preserved when persistent storage returns", async () => {
  const local = memoryStorage();
  const session = memoryStorage();
  const storageKey = "sro-upload-batch-v1";
  const files = [fingerprintable("legacy-pending.pdf", "pending upload from fallback storage")];
  const fingerprint = await legacyFilesFingerprint(files, webCrypto());
  const legacyBaseKey = "c".repeat(48);
  session.setItem(storageKey, JSON.stringify({
    version: 3,
    chunkKeys: {},
    legacyBatches: { [fingerprint]: legacyBaseKey },
    operations: {},
  }));

  const resolved = await resolveBatchKeys(files, "d".repeat(48), 20, {
    cryptoApi: webCrypto(), storage: local, fallbackStorage: session,
    legacyStorage: session, storageKey,
  });

  assert.deepEqual(resolved.keys, [await chunkIdempotencyKey(legacyBaseKey, 0, webCrypto())]);
  assert.equal(session.getItem(storageKey), null);
});

test("intentional reprocessing gets a fresh key, while unknown prior outcomes cannot be bypassed", async () => {
  const storage = memoryStorage();
  const options = { cryptoApi: webCrypto(), storage, storageKey: "explicit-new-run" };
  const files = [fingerprintable("same.jpg", "same image bytes")];
  const first = await resolveBatchKeys(files, "a".repeat(48), 20, options);
  await assert.rejects(
    resolveBatchKeys(files, "b".repeat(48), 20, { ...options, newRun: true }),
    /Неизвестен результат предыдущей отправки/,
  );
  rememberOperation(options, first.chunkFingerprints[0], 61);
  const newRun = await resolveBatchKeys(files, "c".repeat(48), 20, {
    ...options, newRun: true,
  });
  assert.notEqual(newRun.keys[0], first.keys[0]);
  assert.deepEqual(newRun.operationIds, [null]);
  const retry = await resolveBatchKeys(files, "d".repeat(48), 20, options);
  assert.deepEqual(retry.keys, newRun.keys);
});

test("intentional reprocessing cannot bypass an unresolved legacy whole-batch key", async () => {
  const storage = memoryStorage();
  const storageKey = "legacy-new-run-guard";
  const files = [fingerprintable("same.pdf", "legacy outcome not reconciled")];
  const fingerprint = await legacyFilesFingerprint(files, webCrypto());
  const stored = {
    version: 2,
    entries: { [fingerprint]: "e".repeat(48) },
  };
  storage.setItem(storageKey, JSON.stringify(stored));

  await assert.rejects(
    resolveBatchKeys(files, "f".repeat(48), 20, {
      cryptoApi: webCrypto(), storage, storageKey, newRun: true,
    }),
    /Неизвестен результат предыдущей отправки/,
  );
  assert.deepEqual(JSON.parse(storage.getItem(storageKey)), stored);
});

test("shared transport preflight refuses unresolved retry keys without mutating their ledger", async () => {
  const storage = memoryStorage();
  const options = { cryptoApi: webCrypto(), storage, storageKey: "shared-preflight" };
  const files = [fingerprintable("same.pdf", "unknown legacy upload outcome")];
  const pending = await resolveBatchKeys(files, "1".repeat(48), 20, options);
  const beforeCheck = storage.getItem(options.storageKey);
  await assert.rejects(
    resolveBatchKeys(files, "2".repeat(48), 20, {
      ...options, checkOnly: true, requireResolved: true,
    }),
    /Неизвестен результат предыдущей отправки/,
  );
  assert.equal(storage.getItem(options.storageKey), beforeCheck);

  rememberOperation(options, pending.chunkFingerprints[0], 81);
  const confirmedState = storage.getItem(options.storageKey);
  const check = await resolveBatchKeys(files, "3".repeat(48), 20, {
    ...options, checkOnly: true, requireResolved: true,
  });
  assert.equal(check.hasUnresolved, false);
  assert.equal(storage.getItem(options.storageKey), confirmedState);
});

test("confirmed retry history is bounded without dropping unresolved keys", async () => {
  const storage = memoryStorage();
  const chunkKeys = {};
  const operations = {};
  for (let index = 1; index <= 110; index += 1) {
    const fingerprint = index.toString(16).padStart(64, "0");
    chunkKeys[fingerprint] = index.toString(16).padStart(48, "0");
    operations[fingerprint] = index;
  }
  const unresolvedFingerprint = "f".repeat(64);
  chunkKeys[unresolvedFingerprint] = "e".repeat(48);
  storage.setItem("bounded-history", JSON.stringify({ version: 3, chunkKeys, legacyBatches: {}, operations }));
  const files = [fingerprintable("current.pdf", "current")];
  await resolveBatchKeys(files, "a".repeat(48), 20, {
    cryptoApi: webCrypto(), storage, storageKey: "bounded-history",
  });
  const saved = JSON.parse(storage.getItem("bounded-history"));
  assert.equal(Object.keys(saved.chunkKeys).length, 102);
  assert.equal(saved.chunkKeys[unresolvedFingerprint], "e".repeat(48));
  assert.equal(Object.keys(saved.operations).length, 100);
});

test("the upload form requests operation JSON and opens the selected batch dashboard", async () => {
  const files = [fingerprintable("accepted.pdf", "synthetic document")];
  let submitHandler;
  let redirect = "";
  let requestOptions;
  const fileInput = { files, disabled: false };
  const button = { disabled: false, textContent: "Загрузить" };
  const idempotencyInput = { value: "a".repeat(48) };
  const form = {
    action: "http://127.0.0.1:18080/upload",
    dataset: { maxFiles: "100", maxFileBytes: "1000", maxBatchBytes: "1000", filesPerRequest: "20" },
    querySelector(selector) {
      if (selector === 'input[type="file"]') return fileInput;
      if (selector === 'button[type="submit"]') return button;
      if (selector === 'input[name="idempotency_key"]') return idempotencyInput;
      return null;
    },
    addEventListener(type, handler) { if (type === "submit") submitHandler = handler; },
  };
  const status = { textContent: "" };
  const document = { addEventListener() {}, querySelector: (selector) => selector === "form#document-upload-form" ? form : status };
  const storage = memoryStorage();
  const root = {
    addEventListener() {},
    crypto: webCrypto(),
    TextEncoder,
    setTimeout: (callback) => callback(),
    fetch: async (_url, options) => {
      requestOptions = options;
      return {
        ok: true,
        status: 200,
        url: "http://127.0.0.1:18080/upload",
        json: async () => ({ operation_id: 77, task_ids: [91] }),
      };
    },
    location: { href: "http://127.0.0.1:18080/debtors/intake?registry=organizations", assign: (url) => { redirect = url; } },
    localStorage: storage,
    sessionStorage: memoryStorage(),
  };
  root.document = document;
  runInNewContext(readFileSync(new URL("../app/web/static/upload-batch.js", import.meta.url), "utf8"), {
    globalThis: root,
    document,
    module: undefined,
    TextEncoder,
    URL,
    FormData: FakeFormData,
  });
  await submitHandler({ preventDefault() {} });
  assert.equal(requestOptions.headers.Accept, "application/json");
  assert.equal(redirect, "/debtors/intake?intake=77&registry=organizations");
  assert.equal(button.textContent, "Принято");
  const retained = JSON.parse(storage.getItem("sro-upload-batch-v1"));
  assert.equal(Object.values(retained.operations)[0], 77);
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
    operationIds: [null, null, null],
    formDataFactory: () => new FakeFormData(),
    fetchImpl: async (_url, request) => {
      const fields = request.body.values;
      const key = fields.find(([name]) => name === "idempotency_key")[1];
      keysPerAttempt.push(key);
      if (failThirdChunk && keysPerAttempt.length === 3) throw new Error("lost response");
      return {
        ok: true,
        status: 200,
        url: "http://127.0.0.1:18080/upload",
        json: async () => ({
          operation_id: keysPerAttempt.indexOf(key) + 100,
          task_ids: Array(fields.filter(([name]) => name === "files").length).fill(1).map((_, i) => i + 1),
        }),
      };
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
  assert.deepEqual(retry.operationIds, [100, 101, 102]);
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
    fetchImpl: async () => ({ ok: true, status: 200, url: "http://127.0.0.1:18080/login", json: async () => ({}) }),
  });
  assert.equal(result.complete, false);
  assert.equal(result.unknownOutcome, true);
  assert.equal(result.acceptedFiles, 0);
});

test("confirmed operation IDs survive reload metadata and follow chunk order", async () => {
  const storage = memoryStorage();
  const storageKey = "sro-upload-batch-v1";
  const options = { cryptoApi: webCrypto(), storage, storageKey };
  const files = [
    fingerprintable("one.pdf", "one"),
    fingerprintable("two.pdf", "two"),
    fingerprintable("three.pdf", "three"),
  ];
  const keys = await resolveBatchKeys(files, "a".repeat(48), 2, options);
  rememberOperation(options, keys.chunkFingerprints[0], 41);
  const resumed = await resolveBatchKeys(files, "b".repeat(48), 2, options);
  assert.deepEqual(resumed.operationIds, [41, null]);

  const operationByKey = new Map();
  let operationNumber = 41;
  const result = await uploadChunks({
    action: "http://127.0.0.1:18080/upload",
    files,
    filesPerRequest: 2,
    idempotencyKeys: resumed.keys,
    operationIds: resumed.operationIds,
    formDataFactory: () => new FakeFormData(),
    fetchImpl: async (_url, request) => {
      const key = request.body.values.find(([name]) => name === "idempotency_key")[1];
      if (!operationByKey.has(key)) operationByKey.set(key, operationNumber++);
      return {
        ok: true,
        status: 200,
        url: "http://127.0.0.1:18080/upload",
        json: async () => ({
          operation_id: operationByKey.get(key),
          task_ids: Array(request.body.values.filter(([name]) => name === "files").length).fill(5),
        }),
      };
    },
    onOperation: (operationId, index) => rememberOperation(options, resumed.chunkFingerprints[index], operationId),
  });
  assert.equal(result.complete, true);
  assert.deepEqual(result.operationIds, [41, 42]);
  assert.deepEqual(JSON.parse(storage.getItem(storageKey)).operations, {
    [resumed.chunkFingerprints[0]]: 41,
    [resumed.chunkFingerprints[1]]: 42,
  });
});

test("a completed same-selection replay keeps its keys and accepted operation ID", async () => {
  const storage = memoryStorage();
  const storageKey = "sro-upload-batch-v1";
  const options = { cryptoApi: webCrypto(), storage, storageKey };
  const files = [fingerprintable("completed.pdf", "synthetic completed document")];
  const initial = await resolveBatchKeys(files, "1".repeat(48), 20, options);
  const operationId = 701;
  const firstResult = await uploadChunks({
    action: "http://127.0.0.1:18080/upload",
    files,
    filesPerRequest: 20,
    idempotencyKeys: initial.keys,
    operationIds: initial.operationIds,
    formDataFactory: () => new FakeFormData(),
    fetchImpl: async () => ({
      ok: true,
      status: 200,
      url: "http://127.0.0.1:18080/upload",
      json: async () => ({ operation_id: operationId, task_ids: [702] }),
    }),
    onOperation: (id, index) => rememberOperation(options, initial.chunkFingerprints[index], id),
  });
  assert.equal(firstResult.complete, true);

  const reselected = await resolveBatchKeys(
    [fingerprintable("completed.pdf", "synthetic completed document")],
    "2".repeat(48),
    20,
    options,
  );
  assert.deepEqual(reselected.keys, initial.keys);
  assert.deepEqual(reselected.operationIds, [operationId]);
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

test("shared transport manifests bind ordered names sizes and exact content hashes", async () => {
  const files = [fingerprintable("первый.pdf", "synthetic one"), fingerprintable("second.pdf", "synthetic two")];
  const manifest = await transportManifest(files, webCrypto());
  assert.deepEqual(manifest.map(({ filename, size }) => ({ filename, size })), [
    { filename: "первый.pdf", size: files[0].size },
    { filename: "second.pdf", size: files[1].size },
  ]);
  assert.equal(manifest[0].sha256.length, 64);
  assert.notEqual(manifest[0].sha256, manifest[1].sha256);
});

test("shared transport resume skips accepted chunks and submits only the missing chunk", async () => {
  const files = [
    fingerprintable("one.pdf", "one"),
    fingerprintable("two.pdf", "two"),
    fingerprintable("three.pdf", "three"),
  ];
  const batch = {
    id: "a".repeat(32),
    file_count: 3,
    files_per_chunk: 2,
    chunks: [{ index: 0, accepted: true }, { index: 1, accepted: false }],
  };
  const requests = [];
  const progress = [];
  const acceptedBatches = [];
  const events = [];
  const eventTarget = {
    CustomEvent: class CustomEvent { constructor(type, init) { this.type = type; this.detail = init.detail; } },
    dispatchEvent: (event) => { events.push(event); return true; },
  };
  const result = await uploadTransportChunks({
    endpoint: "http://127.0.0.1:18080/upload",
    batch,
    files,
    formDataFactory: () => new FakeFormData(),
    fetchImpl: async (url, options) => {
      requests.push({ url, fields: options.body.values });
      return {
        ok: true,
        status: 200,
        url,
        json: async () => ({ batch_id: batch.id, chunk_index: 1, operation_id: 501, task_ids: [502] }),
      };
    },
    onProgress: (accepted, total) => progress.push([accepted, total]),
    onComplete: (acceptedBatch) => {
      acceptedBatches.push(acceptedBatch.id);
      dispatchMixedUploadAccepted(eventTarget, acceptedBatch);
    },
  });

  assert.equal(result.complete, true);
  assert.equal(result.acceptedFiles, 3);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, `http://127.0.0.1:18080/upload-batches/${batch.id}/chunks/1`);
  assert.deepEqual(requests[0].fields.map((field) => field[2]), ["three.pdf"]);
  assert.deepEqual(progress, [[3, 3]]);
  assert.deepEqual(acceptedBatches, [batch.id]);
  assert.equal(events.length, 1);
  assert.equal(events[0].type, "sro:mixed-upload-accepted");
  assert.deepEqual(events[0].detail, { batch_id: batch.id });
});

test("shared transport does not dispatch mixed acceptance when any chunk is unconfirmed", async () => {
  const batch = {
    id: "b".repeat(32),
    file_count: 1,
    files_per_chunk: 1,
    chunks: [{ index: 0, accepted: false }],
  };
  const events = [];
  const eventTarget = {
    CustomEvent: class CustomEvent { constructor(type, init) { this.type = type; this.detail = init.detail; } },
    dispatchEvent: (event) => { events.push(event); return true; },
  };
  const result = await uploadTransportChunks({
    endpoint: "http://127.0.0.1:18080/upload",
    batch,
    files: [fingerprintable("one.heic", "synthetic heic")],
    formDataFactory: () => new FakeFormData(),
    fetchImpl: async () => ({ ok: false, status: 422, url: "http://127.0.0.1:18080/upload-batches" }),
    onComplete: (acceptedBatch) => dispatchMixedUploadAccepted(eventTarget, acceptedBatch),
  });
  assert.equal(result.complete, false);
  assert.deepEqual(events, []);
});


test("one submission hashes each file once; later submissions read fresh bytes", async () => {
  let reads = 0;
  const file = fingerprintable("synthetic.pdf", "first");
  const read = file.arrayBuffer;
  file.arrayBuffer = () => { reads += 1; return read(); };
  const fingerprints = new WeakMap();
  await resolveBatchKeys([file], "a".repeat(48), 1, {
    cryptoApi: webCrypto(), storage: memoryStorage(), storageKey: "synthetic",
    checkOnly: true, fingerprints,
  });
  const first = await transportManifest([file], webCrypto(), fingerprints);
  assert.equal(reads, 1);
  file.arrayBuffer = async () => { reads += 1; return new TextEncoder().encode("other").buffer; };
  const second = await transportManifest([file], webCrypto(), new WeakMap());
  assert.equal(reads, 2);
  assert.notEqual(first[0].sha256, second[0].sha256);
});
