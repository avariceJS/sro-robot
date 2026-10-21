(function (root) {
  "use strict";

  function chunkFiles(files, maximumPerRequest) {
    if (!Number.isInteger(maximumPerRequest) || maximumPerRequest < 1) {
      throw new TypeError("Invalid per-request file limit");
    }
    const chunks = [];
    for (let start = 0; start < files.length; start += maximumPerRequest) {
      chunks.push(files.slice(start, start + maximumPerRequest));
    }
    return chunks;
  }

  function validateFiles(files, limits) {
    if (!files.length) return "Выберите хотя бы один файл.";
    if (files.length > limits.maxFiles) {
      return `За один раз можно выбрать не более ${limits.maxFiles} файлов.`;
    }
    let totalBytes = 0;
    const supportedSuffixes = new Set([".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"]);
    for (const file of files) {
      const suffix = file.name.slice(file.name.lastIndexOf(".")).toLowerCase();
      if (!supportedSuffixes.has(suffix)) {
        return "Поддерживаются PDF, JPEG, PNG, TIFF, BMP и WebP.";
      }
      if (file.size <= 0) return "Пустые файлы не принимаются.";
      if (file.name.length > 255) return `Имя файла «${file.name.slice(0, 40)}…» слишком длинное.`;
      if (file.size > limits.maxFileBytes) {
        return `Файл «${file.name}» превышает допустимый размер.`;
      }
      totalBytes += file.size;
      if (totalBytes > limits.maxBatchBytes) {
        return `Общий размер файлов превышает ${Math.floor(limits.maxBatchBytes / (1024 * 1024))} МиБ.`;
      }
    }
    return null;
  }

  async function chunkIdempotencyKey(baseKey, chunkIndex, cryptoApi) {
    if (!/^[0-9a-f]{48}$/.test(baseKey) || !cryptoApi || !cryptoApi.subtle) {
      throw new Error("Не удалось подготовить ключ безопасного повтора.");
    }
    const bytes = new TextEncoder().encode(`${baseKey}:${chunkIndex}`);
    const digest = await cryptoApi.subtle.digest("SHA-256", bytes);
    return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0"))
      .join("")
      .slice(0, 48);
  }

  async function chunkSelectionIdempotencyKey(baseKey, chunkFingerprint, cryptoApi) {
    if (!/^[0-9a-f]{48}$/.test(baseKey) || !/^[0-9a-f]{64}$/.test(chunkFingerprint)
      || !cryptoApi || !cryptoApi.subtle) {
      throw new Error("Не удалось подготовить ключ безопасного повтора.");
    }
    const bytes = new TextEncoder().encode(`${baseKey}:${chunkFingerprint}`);
    const digest = await cryptoApi.subtle.digest("SHA-256", bytes);
    return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0"))
      .join("")
      .slice(0, 48);
  }

  async function fileFingerprintEntries(files, cryptoApi) {
    if (!cryptoApi || !cryptoApi.subtle) throw new Error("Не удалось проверить выбранные файлы.");
    const entries = [];
    for (const file of files) {
      const digest = await cryptoApi.subtle.digest("SHA-256", await file.arrayBuffer());
      entries.push({
        name: file.name,
        size: file.size,
        lastModified: file.lastModified,
        sha256: Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join(""),
      });
    }
    return entries;
  }

  async function entriesFingerprint(entries, cryptoApi) {
    const bytes = new TextEncoder().encode(JSON.stringify(entries));
    const digest = await cryptoApi.subtle.digest("SHA-256", bytes);
    return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
  }

  function requestIdentityEntries(entries) {
    return entries.map(({ name, size, sha256 }) => ({ name, size, sha256 }));
  }

  async function filesFingerprint(files, cryptoApi) {
    return entriesFingerprint(
      requestIdentityEntries(await fileFingerprintEntries(files, cryptoApi)),
      cryptoApi,
    );
  }

  async function transportManifest(files, cryptoApi) {
    const entries = await fileFingerprintEntries(files, cryptoApi);
    return requestIdentityEntries(entries).map(({ name, size, sha256 }) => ({
      filename: name,
      size,
      sha256,
    }));
  }

  async function registerTransportBatch(options) {
    const response = await options.fetchImpl(options.endpoint, {
      method: "POST",
      credentials: "same-origin",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify({
        request_key: options.requestKey,
        files_per_chunk: options.filesPerChunk,
        files: options.files,
      }),
    });
    if (!response.ok) throw new Error(`Не удалось зарегистрировать план загрузки (HTTP ${response.status}).`);
    return response.json();
  }

  async function uploadTransportChunks(options) {
    const acceptedFiles = options.batch.chunks
      .filter((chunk) => chunk.accepted)
      .reduce((sum, chunk) => sum + Math.min(
        options.batch.files_per_chunk,
        options.batch.file_count - chunk.index * options.batch.files_per_chunk,
      ), 0);
    let completed = acceptedFiles;
    for (const chunk of options.batch.chunks) {
      if (chunk.accepted) continue;
      const start = chunk.index * options.batch.files_per_chunk;
      const selected = options.files.slice(start, start + options.batch.files_per_chunk);
      const formData = options.formDataFactory();
      for (const file of selected) formData.append("files", file, file.name);
      const endpoint = new URL(options.endpoint);
      endpoint.pathname = `${endpoint.pathname.replace(/\/upload\/?$/, "/upload-batches")}/${options.batch.id}/chunks/${chunk.index}`;
      let response;
      try {
        response = await options.fetchImpl(endpoint.toString(), {
          method: "POST",
          credentials: "same-origin",
          redirect: "follow",
          headers: { Accept: "application/json" },
          body: formData,
        });
      } catch (_error) {
        return { complete: false, unknownOutcome: true, acceptedFiles: completed, failedChunk: chunk.index };
      }
      const responsePath = new URL(response.url || endpoint.toString(), endpoint.toString()).pathname;
      if (!response.ok || responsePath !== endpoint.pathname) {
        return {
          complete: false,
          unknownOutcome: response.status >= 500 || responsePath === "/login",
          acceptedFiles: completed,
          failedChunk: chunk.index,
          httpStatus: response.status,
        };
      }
      const payload = await response.json();
      if (!payload || payload.batch_id !== options.batch.id || payload.chunk_index !== chunk.index
        || !Number.isSafeInteger(payload.operation_id) || payload.operation_id <= 0
        || !Array.isArray(payload.task_ids) || payload.task_ids.length !== selected.length) {
        return { complete: false, unknownOutcome: true, acceptedFiles: completed, failedChunk: chunk.index };
      }
      chunk.accepted = true;
      chunk.operation_id = payload.operation_id;
      completed += selected.length;
      if (options.onProgress) options.onProgress(completed, options.batch.file_count);
      if (options.refreshBatches) await options.refreshBatches();
    }
    return { complete: true, unknownOutcome: false, acceptedFiles: completed };
  }

  async function chunkFingerprint(entries, chunkIndex, cryptoApi) {
    const bytes = new TextEncoder().encode(
      `${chunkIndex}:${JSON.stringify(requestIdentityEntries(entries))}`,
    );
    const digest = await cryptoApi.subtle.digest("SHA-256", bytes);
    return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
  }

  function storageCanRoundTrip(storage, storageKey) {
    if (!storage || typeof storage.getItem !== "function" || typeof storage.setItem !== "function") return false;
    const probeKey = `${storageKey}:probe`;
    try {
      storage.setItem(probeKey, "1");
      if (storage.getItem(probeKey) !== "1") {
        if (typeof storage.removeItem === "function") storage.removeItem(probeKey);
        return false;
      }
      if (typeof storage.removeItem === "function") storage.removeItem(probeKey);
      return true;
    } catch (_error) {
      try { storage.removeItem(probeKey); } catch (_ignored) { /* best effort */ }
      return false;
    }
  }

  async function resolveBatchKeys(files, generatedKey, filesPerRequest, options) {
    const primaryStorage = options.storage;
    const fallbackStorage = options.fallbackStorage;
    const primaryWritable = storageCanRoundTrip(primaryStorage, options.storageKey);
    const sourceStorage = primaryWritable ? primaryStorage : (fallbackStorage || primaryStorage);
    const otherStorage = sourceStorage === primaryStorage ? fallbackStorage : primaryStorage;
    const entries = await fileFingerprintEntries(files, options.cryptoApi);
    const fingerprint = await entriesFingerprint(requestIdentityEntries(entries), options.cryptoApi);
    const legacyFingerprint = await entriesFingerprint(entries, options.cryptoApi);
    const chunks = chunkFiles(files, filesPerRequest);
    const chunkFingerprints = [];
    for (let index = 0; index < chunks.length; index += 1) {
      const start = index * filesPerRequest;
      chunkFingerprints.push(
        await chunkFingerprint(entries.slice(start, start + filesPerRequest), index, options.cryptoApi),
      );
    }
    let stored = null;
    try { stored = JSON.parse(sourceStorage?.getItem(options.storageKey) || "null"); } catch (_error) { stored = null; }
    let fallbackStored = null;
    try { fallbackStored = JSON.parse(otherStorage?.getItem(options.storageKey) || "null"); } catch (_error) { fallbackStored = null; }
    let legacyStored = null;
    if (options.legacyStorage) {
      try {
        legacyStored = JSON.parse(options.legacyStorage.getItem(options.storageKey) || "null");
      } catch (_error) {
        legacyStored = null;
      }
    }
    const chunkKeys = {};
    const operations = {};
    const legacyBatches = {};
    if (stored && stored.version === 3 && stored.chunkKeys && typeof stored.chunkKeys === "object") {
      for (const [chunkFingerprint, key] of Object.entries(stored.chunkKeys)) {
        if (/^[0-9a-f]{64}$/.test(chunkFingerprint) && /^[0-9a-f]{48}$/.test(key)) {
          chunkKeys[chunkFingerprint] = key;
        }
      }
      if (stored.legacyBatches && typeof stored.legacyBatches === "object") {
        for (const [batchFingerprint, baseKey] of Object.entries(stored.legacyBatches)) {
          if (/^[0-9a-f]{64}$/.test(batchFingerprint) && /^[0-9a-f]{48}$/.test(baseKey)) {
            legacyBatches[batchFingerprint] = baseKey;
          }
        }
      }
      if (stored.operations && typeof stored.operations === "object") {
        for (const [chunkFingerprint, operationId] of Object.entries(stored.operations)) {
          if (/^[0-9a-f]{64}$/.test(chunkFingerprint)
            && Number.isSafeInteger(operationId) && operationId > 0) {
            operations[chunkFingerprint] = operationId;
          }
        }
      }
    } else if (stored && stored.version === 2 && stored.entries && typeof stored.entries === "object") {
      // Migrate the previous whole-batch map; per-chunk keys are derived below on an exact replay.
      for (const [batchFingerprint, baseKey] of Object.entries(stored.entries)) {
        if (/^[0-9a-f]{64}$/.test(batchFingerprint) && /^[0-9a-f]{48}$/.test(baseKey)) {
          legacyBatches[batchFingerprint] = baseKey;
        }
      }
    } else if (
      stored
      && /^[0-9a-f]{64}$/.test(stored.fingerprint || "")
      && /^[0-9a-f]{48}$/.test(stored.baseKey || "")
    ) {
      legacyBatches[stored.fingerprint] = stored.baseKey;
    }
    if (fallbackStored && fallbackStored.version === 3 && fallbackStored.chunkKeys
      && typeof fallbackStored.chunkKeys === "object") {
      for (const [chunkFingerprint, key] of Object.entries(fallbackStored.chunkKeys)) {
        if (/^[0-9a-f]{64}$/.test(chunkFingerprint) && /^[0-9a-f]{48}$/.test(key)
          && (!primaryWritable || !chunkKeys[chunkFingerprint])) chunkKeys[chunkFingerprint] = key;
      }
      if (fallbackStored.operations && typeof fallbackStored.operations === "object") {
        for (const [chunkFingerprint, operationId] of Object.entries(fallbackStored.operations)) {
          if (/^[0-9a-f]{64}$/.test(chunkFingerprint) && Number.isSafeInteger(operationId)
            && operationId > 0 && (!primaryWritable || !operations[chunkFingerprint])) operations[chunkFingerprint] = operationId;
        }
      }
      if (fallbackStored.legacyBatches && typeof fallbackStored.legacyBatches === "object") {
        for (const [batchFingerprint, baseKey] of Object.entries(fallbackStored.legacyBatches)) {
          if (/^[0-9a-f]{64}$/.test(batchFingerprint) && /^[0-9a-f]{48}$/.test(baseKey)
            && (!primaryWritable || !legacyBatches[batchFingerprint])) legacyBatches[batchFingerprint] = baseKey;
        }
      }
    }
    const validLegacy = legacyStored
      && /^[0-9a-f]{64}$/.test(legacyStored.fingerprint || "")
      && /^[0-9a-f]{48}$/.test(legacyStored.baseKey || "");
    if (validLegacy && !legacyBatches[legacyStored.fingerprint]) {
      legacyBatches[legacyStored.fingerprint] = legacyStored.baseKey;
    }
    const legacyBaseKey = legacyBatches[legacyFingerprint];
    const unresolvedExisting = Boolean(legacyBaseKey) || chunkFingerprints.some((chunkFingerprint) =>
      chunkKeys[chunkFingerprint] && !operations[chunkFingerprint]);
    if (options.requireResolved === true && unresolvedExisting) {
      throw new Error("Неизвестен результат предыдущей отправки. Сначала проверьте её в списке заданий.");
    }
    if (options.checkOnly === true) {
      return { hasUnresolved: unresolvedExisting, fingerprint, chunkFingerprints };
    }
    const keys = [];
    const newRun = options.newRun === true;
    if (newRun) {
      if (unresolvedExisting) {
        throw new Error("Неизвестен результат предыдущей отправки. Сначала проверьте её в списке заданий.");
      }
    }
    let requestBaseKey = generatedKey;
    if (newRun && options.cryptoApi && typeof options.cryptoApi.getRandomValues === "function") {
      const bytes = options.cryptoApi.getRandomValues(new Uint8Array(24));
      requestBaseKey = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
    }
    for (let index = 0; index < chunkFingerprints.length; index += 1) {
      const chunkFingerprint = chunkFingerprints[index];
      const key = (!newRun && chunkKeys[chunkFingerprint])
        || (!newRun && legacyBaseKey
          ? await chunkIdempotencyKey(legacyBaseKey, index, options.cryptoApi)
          : await chunkSelectionIdempotencyKey(requestBaseKey, chunkFingerprint, options.cryptoApi));
      chunkKeys[chunkFingerprint] = key;
      if (newRun) delete operations[chunkFingerprint];
      keys.push(key);
    }
    const migratedBaseKey = legacyBaseKey;
    if (migratedBaseKey && !newRun) delete legacyBatches[legacyFingerprint];
    // Keep unresolved keys indefinitely, but bound old, server-confirmed retry history.
    const confirmed = Object.keys(chunkKeys).filter((key) => operations[key] && !chunkFingerprints.includes(key));
    while (confirmed.length > 100) {
      const expired = confirmed.shift();
      delete chunkKeys[expired];
      delete operations[expired];
    }
    const serialized = JSON.stringify({ version: 3, chunkKeys, legacyBatches, operations });
    let activeStorage = null;
    const candidates = primaryWritable
      ? [primaryStorage, fallbackStorage]
      : [fallbackStorage, primaryStorage];
    for (const candidate of candidates) {
      if (!candidate || typeof candidate.getItem !== "function" || typeof candidate.setItem !== "function") continue;
      try {
        candidate.setItem(options.storageKey, serialized);
        if (candidate.getItem(options.storageKey) !== serialized) throw new Error("Browser storage read-back mismatch");
        activeStorage = candidate;
        break;
      } catch (_error) {
        // Try session storage or the per-page memory adapter before failing the upload.
      }
    }
    if (!activeStorage) throw new Error("Не удалось сохранить ключ безопасного повтора в браузере.");
    if (activeStorage === primaryStorage && otherStorage && otherStorage !== primaryStorage) {
      try { otherStorage.removeItem(options.storageKey); } catch (_error) { /* Primary now has the merged retry state. */ }
    }
    try {
      if (
        validLegacy
        && legacyStored.fingerprint === legacyFingerprint
        && migratedBaseKey === legacyStored.baseKey
        && options.legacyStorage
      ) {
        options.legacyStorage.removeItem(options.storageKey);
      }
    } catch (_error) { /* Old session metadata can be removed on a later successful upload. */ }
    return {
      keys,
      chunkFingerprints,
      fingerprint,
      operationIds: chunkFingerprints.map((chunkFingerprint) => operations[chunkFingerprint] || null),
      storage: activeStorage,
    };
  }

  function rememberOperation(options, chunkFingerprint, operationId) {
    if (!Number.isSafeInteger(operationId) || operationId <= 0) {
      throw new Error("Invalid intake operation identifier");
    }
    const stored = JSON.parse(options.storage.getItem(options.storageKey) || "null");
    if (!stored || stored.version !== 3 || !stored.chunkKeys || typeof stored.chunkKeys !== "object") {
      throw new Error("Retry metadata is unavailable");
    }
    const operations = stored.operations && typeof stored.operations === "object"
      ? { ...stored.operations }
      : {};
    operations[chunkFingerprint] = operationId;
    const serialized = JSON.stringify({
      version: 3,
      chunkKeys: stored.chunkKeys,
      legacyBatches: stored.legacyBatches && typeof stored.legacyBatches === "object"
        ? stored.legacyBatches
        : {},
      operations,
    });
    let savedStorage = null;
    for (const candidate of [options.storage, options.fallbackStorage]) {
      if (!candidate) continue;
      try {
        candidate.setItem(options.storageKey, serialized);
        if (candidate.getItem(options.storageKey) !== serialized) throw new Error("Retry metadata read-back mismatch");
        savedStorage = candidate;
        if (candidate === options.storage && options.fallbackStorage && options.fallbackStorage !== candidate) {
          try { options.fallbackStorage.removeItem(options.storageKey); } catch (_error) { /* best effort */ }
        }
        break;
      } catch (_error) { /* Keep the confirmed response in the fallback storage when possible. */ }
    }
    if (!savedStorage) throw new Error("Retry metadata could not be persisted");
    return savedStorage;
  }

  function clearChunkKeys(options, chunkFingerprints, keys) {
    try {
      const stored = JSON.parse(options.storage.getItem(options.storageKey) || "null");
      if (stored && stored.version === 3 && stored.chunkKeys && typeof stored.chunkKeys === "object") {
        const chunkKeys = { ...stored.chunkKeys };
        const operations = stored.operations && typeof stored.operations === "object"
          ? { ...stored.operations }
          : {};
        for (let index = 0; index < chunkFingerprints.length; index += 1) {
          if (chunkKeys[chunkFingerprints[index]] === keys[index]) {
            delete chunkKeys[chunkFingerprints[index]];
            delete operations[chunkFingerprints[index]];
          }
        }
        const legacyBatches = stored.legacyBatches && typeof stored.legacyBatches === "object"
          ? stored.legacyBatches
          : {};
        if (Object.keys(chunkKeys).length || Object.keys(legacyBatches).length || Object.keys(operations).length) {
          options.storage.setItem(options.storageKey, JSON.stringify({ version: 3, chunkKeys, legacyBatches, operations }));
        } else {
          options.storage.removeItem(options.storageKey);
        }
      }
    } catch (_error) {
      // Clearing browser storage is best-effort; it never changes server state.
    }
  }

  async function uploadChunks(options) {
    const chunks = chunkFiles(options.files, options.filesPerRequest);
    let acceptedFiles = 0;
    const operationIds = (options.operationIds || Array(chunks.length).fill(null)).slice();
    for (let index = 0; index < chunks.length; index += 1) {
      const formData = options.formDataFactory();
      const key = options.idempotencyKeys
        ? options.idempotencyKeys[index]
        : await chunkIdempotencyKey(options.baseKey, index, options.cryptoApi);
      formData.append("idempotency_key", key);
      for (const file of chunks[index]) formData.append("files", file, file.name);

      try {
        const response = await options.fetchImpl(options.action, {
          method: "POST",
          credentials: "same-origin",
          redirect: "follow",
          headers: { Accept: "application/json" },
          body: formData,
        });
        const responsePath = new URL(response.url || options.action, options.action).pathname;
        const actionPath = new URL(options.action).pathname;
        if (!response.ok || responsePath !== actionPath) {
          return {
            complete: false,
            acceptedFiles,
            failedChunk: index + 1,
            totalChunks: chunks.length,
            unknownOutcome: response.status >= 500 || responsePath === "/login",
            httpStatus: response.status,
          };
        }
        const payload = await response.json();
        if (!payload || !Number.isSafeInteger(payload.operation_id) || payload.operation_id <= 0
          || !Array.isArray(payload.task_ids) || payload.task_ids.length !== chunks[index].length
          || payload.task_ids.some((taskId) => !Number.isSafeInteger(taskId) || taskId <= 0)) {
          return {
            complete: false,
            acceptedFiles,
            failedChunk: index + 1,
            totalChunks: chunks.length,
            unknownOutcome: true,
          };
        }
        operationIds[index] = payload.operation_id;
        if (options.onOperation) options.onOperation(payload.operation_id, index);
      } catch (_error) {
        return {
          complete: false,
          acceptedFiles,
          failedChunk: index + 1,
          totalChunks: chunks.length,
          unknownOutcome: true,
        };
      }
      acceptedFiles += chunks[index].length;
      if (options.onProgress) options.onProgress(acceptedFiles, options.files.length);
    }
    return {
      complete: true,
      acceptedFiles,
      totalChunks: chunks.length,
      unknownOutcome: false,
      operationIds,
    };
  }

  const api = {
    chunkFiles,
    validateFiles,
    chunkIdempotencyKey,
    filesFingerprint,
    transportManifest,
    registerTransportBatch,
    uploadTransportChunks,
    resolveBatchKeys,
    rememberOperation,
    clearChunkKeys,
    uploadChunks,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;

  if (typeof document === "undefined") return;
  const form = document.querySelector("form#document-upload-form");
  if (!form) return;
  const fileInput = form.querySelector('input[type="file"]');
  const button = form.querySelector('button[type="submit"]');
  const status = document.querySelector("#document-upload-status");
  const dropZone = document.querySelector("#document-drop-zone");
  const selection = document.querySelector("#drop-selection");
  if (dropZone && typeof dropZone.addEventListener === "function"
    && typeof fileInput.addEventListener === "function") {
    const renderSelection = () => {
      const count = (fileInput.files || []).length;
      if (selection) selection.textContent = count ? `Выбрано файлов: ${count}` : "";
    };
    fileInput.addEventListener("change", renderSelection);
    dropZone.addEventListener("dragover", (event) => {
      event.preventDefault();
      dropZone.classList.add("dragover");
    });
    for (const eventName of ["dragleave", "drop"]) {
      dropZone.addEventListener(eventName, () => dropZone.classList.remove("dragover"));
    }
    dropZone.addEventListener("drop", (event) => {
      event.preventDefault();
      if (event.dataTransfer && event.dataTransfer.files) {
        fileInput.files = event.dataTransfer.files;
        renderSelection();
      }
    });
  }
  let generatedBaseKey = form.querySelector('input[name="idempotency_key"]').value;
  const newRunInput = form.querySelector('input[name="new_processing_run"]');
  // Keep the storage slot name so pending v1 keys survive a code deployment.
  const storageKey = "sro-upload-batch-v1";
  let batchStorage;
  let legacyBatchStorage;
  try {
    batchStorage = root.localStorage;
  } catch (_error) {
    batchStorage = null;
  }
  try {
    legacyBatchStorage = root.sessionStorage;
  } catch (_error) {
    legacyBatchStorage = null;
  }
  const pageStorage = (() => {
    const values = new Map();
    return {
      getItem: (key) => values.has(key) ? values.get(key) : null,
      setItem: (key, value) => values.set(key, String(value)),
      removeItem: (key) => values.delete(key),
    };
  })();
  let batchKeys = null;
  let busy = false;
  let memoryOnly = false;
  const recoveryPanel = typeof document.getElementById === "function"
    ? document.getElementById("upload-batch-recovery")
    : null;
  let selectedTransportBatch = null;
  let selectingTransportBatch = false;
  let batchListPage = 0;
  let listRefreshGeneration = 0;
  let batchSelectionGeneration = 0;
  const linkedResumeBatchId = new URL(
    root.location?.href || root.location?.pathname || "/",
    form.action,
  ).searchParams.get("resume_batch");
  let linkedResumeAttempted = false;
  let linkedResumeState = linkedResumeBatchId === null
    ? "none"
    : (/^[0-9a-f]{32}$/.test(linkedResumeBatchId) ? "pending" : "invalid");

  async function selectTransportBatch(batchId, { linked = false } = {}) {
    const selectionGeneration = ++batchSelectionGeneration;
    selectedTransportBatch = null;
    selectingTransportBatch = true;
    linkedResumeState = "pending";
    status.textContent = linked
      ? "Открываю указанную партию для продолжения…"
      : "Открываю выбранную партию…";
    try {
      const detail = await root.fetch(`${new URL(form.action).origin}/upload-batches/${batchId}`, {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      if (!detail.ok) throw new Error("Batch detail unavailable");
      const selected = await detail.json();
      if (selected.id !== batchId) throw new Error("Batch identity mismatch");
      if (selectionGeneration !== batchSelectionGeneration) return false;
      selectedTransportBatch = selected;
      linkedResumeState = "resolved";
      status.textContent = `Партия выбрана: ${selected.id.slice(0, 8)}. Выберите полный исходный набор из ${selected.file_count} файлов в исходном порядке и нажмите «Загрузить».`;
      return true;
    } catch (_error) {
      if (selectionGeneration === batchSelectionGeneration) {
        linkedResumeState = "failed";
        status.textContent = linked
          ? "Не удалось открыть указанную партию. Отправка заблокирована; обновите список или выберите партию вручную."
          : "Не удалось открыть партию. Список будет обновлён; повторите выбор.";
      }
      return false;
    } finally {
      if (selectionGeneration === batchSelectionGeneration) selectingTransportBatch = false;
    }
  }

  async function openLinkedResumeIfNeeded() {
    if (linkedResumeState === "invalid") {
      linkedResumeAttempted = true;
      linkedResumeState = "failed";
      status.textContent = "Некорректная ссылка продолжения. Отправка заблокирована; выберите партию из общего списка.";
    } else if (linkedResumeBatchId && linkedResumeState === "pending" && !linkedResumeAttempted) {
      linkedResumeAttempted = true;
      await selectTransportBatch(linkedResumeBatchId, { linked: true });
    }
  }

  function freshTransportRequestKey() {
    if (!root.crypto || typeof root.crypto.getRandomValues !== "function") {
      throw new Error("Не удалось подготовить ключ новой партии.");
    }
    return Array.from(root.crypto.getRandomValues(new Uint8Array(24)), (byte) =>
      byte.toString(16).padStart(2, "0"),
    ).join("");
  }

  async function sharedTransportIdentity(files, filesPerChunk) {
    const descriptors = await transportManifest(files, root.crypto);
    const canonical = JSON.stringify({
      files: descriptors.map(({ filename, size, sha256 }) => ({ filename, sha256, size })),
      files_per_chunk: filesPerChunk,
      version: 1,
    });
    const bytes = new TextEncoder().encode(`sro-robot/upload-transport-manifest/v1\0${canonical}`);
    const digest = await root.crypto.subtle.digest("SHA-256", bytes);
    const fingerprint = Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
    return { descriptors, fingerprint, defaultRequestKey: fingerprint.slice(0, 48) };
  }

  function explicitSharedRequestKey(fingerprint) {
    const key = `sro-upload-transport-new-run-v1:${fingerprint}`;
    for (const storage of [batchStorage, pageStorage]) {
      if (!storage) continue;
      try {
        const existing = storage.getItem(key);
        if (existing && /^[0-9a-f]{48}$/.test(existing)) return existing;
        const created = freshTransportRequestKey();
        storage.setItem(key, created);
        return created;
      } catch (_error) {
        // Continue with page memory if browser storage is unavailable.
      }
    }
    return freshTransportRequestKey();
  }

  function pendingExplicitRequestKey(fingerprint) {
    const key = `sro-upload-transport-new-run-v1:${fingerprint}`;
    for (const storage of [batchStorage, pageStorage]) {
      if (!storage) continue;
      try {
        const existing = storage.getItem(key);
        if (existing && /^[0-9a-f]{48}$/.test(existing)) return existing;
      } catch (_error) {
        // Continue to the in-memory fallback if browser storage is unavailable.
      }
    }
    return null;
  }

  function currentSharedBatchId(fingerprint) {
    const key = `sro-upload-transport-current-v1:${fingerprint}`;
    for (const storage of [batchStorage, pageStorage]) {
      if (!storage) continue;
      try {
        const existing = storage.getItem(key);
        if (existing && /^[0-9a-f]{32}$/.test(existing)) return existing;
      } catch (_error) {
        // Fall back to registering the deterministic manifest identity.
      }
    }
    return null;
  }

  function rememberCurrentSharedBatchId(fingerprint, batchId) {
    const key = `sro-upload-transport-current-v1:${fingerprint}`;
    for (const storage of [batchStorage, pageStorage]) {
      if (!storage) continue;
      try {
        storage.setItem(key, batchId);
        return true;
      } catch (_error) {
        // Continue to the in-memory fallback if browser storage is unavailable.
      }
    }
    return false;
  }

  function forgetPendingExplicitRequestKey(fingerprint, expectedRequestKey) {
    const key = `sro-upload-transport-new-run-v1:${fingerprint}`;
    if (!/^[0-9a-f]{48}$/.test(expectedRequestKey || "")) return;
    for (const storage of [batchStorage, pageStorage]) {
      if (!storage) continue;
      try {
        if (storage.getItem(key) === expectedRequestKey) storage.removeItem(key);
      } catch (_error) {
        // Continue to the in-memory fallback if browser storage is unavailable.
      }
    }
  }

  async function clearPendingExplicitRequestKeyForBatch(fingerprint, batch) {
    const requestKey = pendingExplicitRequestKey(fingerprint);
    if (!requestKey || !batch || !/^[0-9a-f]{64}$/.test(batch.request_key_sha256 || "")) return;
    const prefix = new TextEncoder().encode("sro-robot/upload-transport-request-key/v1\0");
    const rawKey = Uint8Array.from(requestKey.match(/.{2}/g), (byte) => Number.parseInt(byte, 16));
    const bytes = new Uint8Array(prefix.length + rawKey.length);
    bytes.set(prefix);
    bytes.set(rawKey, prefix.length);
    const digest = await root.crypto.subtle.digest("SHA-256", bytes);
    const fingerprintOfKey = Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
    if (fingerprintOfKey === batch.request_key_sha256) {
      forgetPendingExplicitRequestKey(fingerprint, requestKey);
    }
  }

  async function refreshTransportBatches() {
    if (!recoveryPanel) return;
    const generation = ++listRefreshGeneration;
    try {
      const response = await root.fetch(`${new URL(form.action).origin}/upload-batches?page=${batchListPage}`, {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) throw new Error("Batch list unavailable");
      const payload = await response.json();
      if (generation !== listRefreshGeneration) return;
      recoveryPanel.replaceChildren();
      const heading = document.createElement("h3");
      heading.textContent = "Общие партии загрузки";
      recoveryPanel.appendChild(heading);
      const batches = Array.isArray(payload.batches) ? payload.batches : [];
      const renderPager = () => {
        const pager = document.createElement("p");
        pager.className = "upload-batch-pagination";
        if (batchListPage > 0) {
          const newer = document.createElement("button");
          newer.type = "button";
          newer.className = "btn-secondary";
          newer.textContent = "Новее →";
          newer.addEventListener("click", () => {
            batchListPage = Math.max(0, batchListPage - 1);
            refreshTransportBatches();
          });
          pager.appendChild(newer);
        }
        if (payload.has_more) {
          const older = document.createElement("button");
          older.type = "button";
          older.className = "btn-secondary";
          older.textContent = "Старее →";
          older.addEventListener("click", () => {
            batchListPage += 1;
            refreshTransportBatches();
          });
          pager.appendChild(older);
        }
        if (pager.childElementCount) recoveryPanel.appendChild(pager);
      };
      if (!batches.length) {
        const empty = document.createElement("p");
        empty.className = "muted";
        empty.textContent = batchListPage
          ? "На этой странице больше нет партий загрузки."
          : "Общих партий загрузки пока нет.";
        recoveryPanel.appendChild(empty);
        renderPager();
        await openLinkedResumeIfNeeded();
        return;
      }
      renderPager();
      for (const batch of batches) {
        const row = document.createElement("p");
        const accepted = batch.chunks.filter((chunk) => chunk.accepted).length;
        const button = document.createElement("button");
        button.type = "button";
        button.className = "btn-secondary";
        button.textContent = `${batch.id.slice(0, 8)} · принято ${accepted} из ${batch.chunk_count} частей · ${batch.file_count} файлов`;
        button.setAttribute("aria-pressed", String(selectedTransportBatch?.id === batch.id));
        button.addEventListener("click", async () => {
          if (busy) return;
          button.disabled = true;
          await selectTransportBatch(batch.id);
          button.disabled = false;
          refreshTransportBatches();
        });
        row.appendChild(button);
        const workLink = document.createElement("a");
        workLink.className = "btn";
        workLink.href = `${new URL(form.action).origin}/upload-batches/${batch.id}/work`;
        workLink.textContent = "Открыть принятые задания";
        row.appendChild(document.createTextNode(" "));
        row.appendChild(workLink);
        recoveryPanel.appendChild(row);
      }
      await openLinkedResumeIfNeeded();
    } catch (_error) {
      if (generation !== listRefreshGeneration) return;
      recoveryPanel.textContent = "Список незавершённых партий временно недоступен.";
      if (linkedResumeState === "pending" || linkedResumeState === "invalid") {
        linkedResumeAttempted = true;
        linkedResumeState = "failed";
        status.textContent = "Не удалось открыть список и проверить указанную партию. Отправка заблокирована до повторной проверки.";
      }
    }
  }

  async function processSharedTransport(files, selectedBatchAtSubmit, newProcessingRun) {
    const filesPerRequest = Number(form.dataset.filesPerRequest);
    const identity = await sharedTransportIdentity(files, filesPerRequest);
    const descriptors = identity.descriptors;
    const requestKey = newProcessingRun
      ? explicitSharedRequestKey(identity.fingerprint)
      : identity.defaultRequestKey;
    let batch = selectedBatchAtSubmit;
    if (batch) {
      if (JSON.stringify(descriptors) !== JSON.stringify(batch.files)) {
        throw new Error("Файлы должны точно совпадать с исходной партией и идти в исходном порядке.");
      }
      const response = await root.fetch(`${new URL(form.action).origin}/upload-batches/${batch.id}`, {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) throw new Error("Не удалось обновить состояние партии. Проверьте историю загрузок перед повтором.");
      batch = await response.json();
    } else {
      const currentBatchId = !newProcessingRun && currentSharedBatchId(identity.fingerprint);
      if (currentBatchId) {
        const response = await root.fetch(`${new URL(form.action).origin}/upload-batches/${currentBatchId}`, {
          credentials: "same-origin",
          headers: { Accept: "application/json" },
        });
        if (response.ok) {
          batch = await response.json();
          if (JSON.stringify(descriptors) !== JSON.stringify(batch.files)) {
            throw new Error("Сохранённая партия не совпадает с выбранными файлами. Проверьте список партий.");
          }
        } else if (response.status !== 404) {
          throw new Error("Не удалось проверить сохранённую партию. Проверьте историю перед повтором.");
        }
      }
      if (!batch) {
        batch = await registerTransportBatch({
          endpoint: `${new URL(form.action).origin}/upload-batches`,
          requestKey,
          filesPerChunk: filesPerRequest,
          files: descriptors,
          fetchImpl: root.fetch.bind(root),
        });
      }
      selectedTransportBatch = batch;
    }
    const batchIdentityStored = rememberCurrentSharedBatchId(identity.fingerprint, batch.id);
    if (batchIdentityStored) await clearPendingExplicitRequestKeyForBatch(identity.fingerprint, batch);
    const result = await uploadTransportChunks({
      endpoint: form.action,
      batch,
      files,
      formDataFactory: () => new FormData(),
      fetchImpl: root.fetch.bind(root),
      onProgress: (accepted, total) => { status.textContent = `Принято ${accepted} из ${total} файлов.`; },
      refreshBatches: refreshTransportBatches,
    });
    await refreshTransportBatches();
    if (result.complete) {
      if (selectedTransportBatch?.id === batch.id) selectedTransportBatch = null;
      rememberCurrentSharedBatchId(identity.fingerprint, batch.id);
      const operationIds = batch.chunks
        .map((chunk) => chunk.operation_id)
        .filter((operationId) => Number.isSafeInteger(operationId) && operationId > 0);
      status.replaceChildren(document.createTextNode("Все части приняты. "));
      if (operationIds.length === batch.chunk_count) {
        const link = document.createElement("a");
        link.href = `${new URL(form.action).origin}/debtors/intake?intake=${operationIds.join(",")}`;
        link.textContent = `Открыть задания для ${result.acceptedFiles} файлов`;
        status.appendChild(link);
      } else {
        status.appendChild(document.createTextNode(`Задания созданы для ${result.acceptedFiles} файлов.`));
      }
    } else {
      status.textContent = result.unknownOutcome
        ? `Ответ на часть ${result.failedChunk + 1} не подтверждён. Выберите эту партию в списке, чтобы продолжить.`
        : `Часть ${result.failedChunk + 1} не принята (HTTP ${result.httpStatus || "ошибка"}). Ранее принятые части сохранены.`;
    }
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy) return;
    if (selectingTransportBatch) {
      status.textContent = "Подождите, пока выбранная партия откроется.";
      return;
    }
    if (["pending", "failed", "invalid"].includes(linkedResumeState)) {
      status.textContent = "Сначала подтвердите точную партию в общем списке. Файлы не отправлены.";
      return;
    }
    const files = Array.from(fileInput.files || []);
    const limits = {
      maxFiles: Number(form.dataset.maxFiles),
      maxFileBytes: Number(form.dataset.maxFileBytes),
      maxBatchBytes: Number(form.dataset.maxBatchBytes),
    };
    const validationError = validateFiles(files, limits);
    if (validationError) {
      status.textContent = validationError;
      return;
    }

    const sharedTransport = Boolean(selectedTransportBatch)
      || linkedResumeState === "resolved"
      || Boolean(form.querySelector('input[name="shared_transport_batch"]')?.checked);
    if (sharedTransport) {
      if (selectedTransportBatch && newRunInput && newRunInput.checked) {
        status.textContent = "Снимите флаг нового запуска, чтобы продолжить выбранную партию. Для нового запуска сначала завершите текущий.";
        return;
      }
      busy = true;
      button.disabled = true;
      fileInput.disabled = true;
      button.textContent = "Загружаю партию…";
      status.textContent = "Проверяю файлы и сохраняю план партии…";
      try {
        await resolveBatchKeys(files, generatedBaseKey, Number(form.dataset.filesPerRequest), {
          cryptoApi: root.crypto,
          storage: batchStorage,
          fallbackStorage: legacyBatchStorage || pageStorage,
          legacyStorage: legacyBatchStorage,
          storageKey,
          newRun: false,
          checkOnly: true,
          requireResolved: true,
        });
        await processSharedTransport(files, selectedTransportBatch, Boolean(newRunInput && newRunInput.checked));
      } catch (error) {
        await refreshTransportBatches();
        status.textContent = error && error.message
          ? error.message
          : "Не удалось продолжить партию. Проверьте её состояние в списке перед повтором.";
      } finally {
        busy = false;
        button.disabled = false;
        fileInput.disabled = false;
        button.textContent = "Загрузить";
      }
      return;
    }

    busy = true;
    button.disabled = true;
    fileInput.disabled = true;
    button.textContent = "Загрузка…";
    status.textContent = `Отправлено 0 из ${files.length} файлов.`;
    let keysReady = false;
    try {
      batchKeys = await resolveBatchKeys(files, generatedBaseKey, Number(form.dataset.filesPerRequest), {
        cryptoApi: root.crypto,
        storage: batchStorage,
        fallbackStorage: legacyBatchStorage || pageStorage,
        legacyStorage: legacyBatchStorage,
        storageKey,
        newRun: Boolean(newRunInput && newRunInput.checked),
      });
      batchStorage = batchKeys.storage;
      memoryOnly = batchStorage === pageStorage;
      keysReady = true;
    } catch (error) {
      var preparationError = error;
    }

    if (!keysReady) {
      busy = false;
      button.disabled = false;
      fileInput.disabled = false;
      button.textContent = "Повторить подготовку загрузки";
      status.textContent = preparationError && preparationError.message
        ? preparationError.message
        : "Не удалось подготовить безопасный повтор. Файлы не отправлялись.";
      return;
    }

    let result;
    try {
      result = await uploadChunks({
        action: form.action,
        idempotencyKeys: batchKeys.keys,
        files,
        filesPerRequest: Number(form.dataset.filesPerRequest),
        operationIds: batchKeys.operationIds,
        onOperation: (operationId, index) => {
          batchKeys.storage = rememberOperation(
            { storage: batchKeys.storage, fallbackStorage: legacyBatchStorage || pageStorage, storageKey },
            batchKeys.chunkFingerprints[index],
            operationId,
          );
        },
        cryptoApi: root.crypto,
        formDataFactory: () => new FormData(),
        fetchImpl: root.fetch.bind(root),
        onProgress: (accepted, total) => {
          status.textContent = `Отправлено ${accepted} из ${total} файлов.`;
        },
      });
    } catch (_error) {
      result = {
        complete: false,
        acceptedFiles: 0,
        failedChunk: 1,
        totalChunks: Math.ceil(files.length / Number(form.dataset.filesPerRequest)),
        unknownOutcome: true,
      };
    }

    busy = false;
    if (result.complete) {
      button.textContent = "Принято";
      status.textContent = `Принято ${result.acceptedFiles} файлов. Открываю список карточек для проверки…`;
      const validOperationIds = result.operationIds.filter(
        (operationId) => Number.isSafeInteger(operationId) && operationId > 0,
      );
      if (validOperationIds.length !== result.totalChunks || validOperationIds.length > 100) {
        button.disabled = false;
        fileInput.disabled = false;
        button.textContent = "Повторить с теми же файлами";
        status.textContent = "Загрузка принята, но список операций не подтверждён. Повтор безопасно запросит прежние операции.";
        return;
      }
      const resultsPath = root.location.pathname === "/debtors/intake" ? "/debtors/intake" : "/";
      root.setTimeout(() => root.location.assign(`${resultsPath}?intake=${validOperationIds.join(",")}`), 500);
      return;
    }

    button.disabled = false;
    fileInput.disabled = false;
    if (!result.unknownOutcome && result.httpStatus >= 400 && result.httpStatus < 500) {
      if (result.acceptedFiles > 0 && batchKeys) {
        const failedIndex = result.failedChunk - 1;
        clearChunkKeys(
          { storage: batchKeys.storage, storageKey },
          [batchKeys.chunkFingerprints[failedIndex]],
          [batchKeys.keys[failedIndex]],
        );
      } else if (batchKeys) {
        clearChunkKeys({ storage: batchKeys.storage, storageKey }, batchKeys.chunkFingerprints, batchKeys.keys);
      }
      button.textContent = "Загрузить исправленные файлы";
      status.textContent = `Сервер отклонил часть ${result.failedChunk} из ${result.totalChunks} (HTTP ${result.httpStatus}). Исправьте выбор и отправьте снова; подтверждённые ${result.acceptedFiles} файлов уже приняты.`;
      return;
    }
    button.textContent = "Повторить с теми же файлами";
    const outcome = result.unknownOutcome ? "Ответ сервера не подтверждён" : "Сервер отклонил часть загрузки";
    const storageNote = memoryOnly ? " Хранилище браузера недоступно: не закрывайте вкладку до подтверждения результата." : "";
    status.textContent = `${outcome}. Подтверждено ${result.acceptedFiles} из ${files.length} файлов; часть ${result.failedChunk} из ${result.totalChunks}. Повтор безопасно использует те же ключи для уже принятых частей.${storageNote}`;
  });

  refreshTransportBatches();

})(typeof globalThis === "undefined" ? window : globalThis);
