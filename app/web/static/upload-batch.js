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
    for (const file of files) {
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

  async function filesFingerprint(files, cryptoApi) {
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
    const bytes = new TextEncoder().encode(JSON.stringify(entries));
    const digest = await cryptoApi.subtle.digest("SHA-256", bytes);
    return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
  }

  async function resolveBatchKey(files, generatedKey, options) {
    const fingerprint = await filesFingerprint(files, options.cryptoApi);
    let stored = null;
    try {
      stored = JSON.parse(options.storage.getItem(options.storageKey) || "null");
    } catch (_error) {
      stored = null;
    }
    let legacyStored = null;
    if (options.legacyStorage) {
      try {
        legacyStored = JSON.parse(options.legacyStorage.getItem(options.storageKey) || "null");
      } catch (_error) {
        legacyStored = null;
      }
    }
    const entries = {};
    if (stored && typeof stored === "object" && stored.entries && typeof stored.entries === "object") {
      for (const [entryFingerprint, entryBaseKey] of Object.entries(stored.entries)) {
        if (/^[0-9a-f]{64}$/.test(entryFingerprint) && /^[0-9a-f]{48}$/.test(entryBaseKey)) {
          entries[entryFingerprint] = entryBaseKey;
        }
      }
    } else if (
      stored
      && /^[0-9a-f]{64}$/.test(stored.fingerprint || "")
      && /^[0-9a-f]{48}$/.test(stored.baseKey || "")
    ) {
      // Migrate the previous single pending batch without losing its retry key.
      entries[stored.fingerprint] = stored.baseKey;
    }
    const validLegacy = legacyStored
      && /^[0-9a-f]{64}$/.test(legacyStored.fingerprint || "")
      && /^[0-9a-f]{48}$/.test(legacyStored.baseKey || "");
    if (validLegacy && !entries[legacyStored.fingerprint]) {
      entries[legacyStored.fingerprint] = legacyStored.baseKey;
    }
    const baseKey = entries[fingerprint] || generatedKey;
    entries[fingerprint] = baseKey;
    try {
      options.storage.setItem(options.storageKey, JSON.stringify({ version: 2, entries }));
      if (
        validLegacy
        && legacyStored.fingerprint === fingerprint
        && entries[fingerprint] === legacyStored.baseKey
        && options.legacyStorage
      ) {
        options.legacyStorage.removeItem(options.storageKey);
      }
    } catch (_error) {
      // Storage may be disabled by the browser. Same-page retries still retain the key in memory.
    }
    return baseKey;
  }

  function clearBatchKey(options, baseKey) {
    try {
      const stored = JSON.parse(options.storage.getItem(options.storageKey) || "null");
      if (stored && stored.entries && typeof stored.entries === "object") {
        const entries = { ...stored.entries };
        for (const [fingerprint, entryBaseKey] of Object.entries(entries)) {
          if (entryBaseKey === baseKey) delete entries[fingerprint];
        }
        if (Object.keys(entries).length) {
          options.storage.setItem(options.storageKey, JSON.stringify({ version: 2, entries }));
        } else {
          options.storage.removeItem(options.storageKey);
        }
      } else if (stored && stored.baseKey === baseKey) {
        options.storage.removeItem(options.storageKey);
      }
      if (options.legacyStorage) {
        const legacyStored = JSON.parse(options.legacyStorage.getItem(options.storageKey) || "null");
        if (legacyStored && legacyStored.baseKey === baseKey) {
          options.legacyStorage.removeItem(options.storageKey);
        }
      }
    } catch (_error) {
      // Clearing browser storage is best-effort; it never changes server state.
    }
  }

  async function uploadChunks(options) {
    const chunks = chunkFiles(options.files, options.filesPerRequest);
    let acceptedFiles = 0;
    for (let index = 0; index < chunks.length; index += 1) {
      const formData = options.formDataFactory();
      formData.append(
        "idempotency_key",
        await chunkIdempotencyKey(options.baseKey, index, options.cryptoApi),
      );
      for (const file of chunks[index]) formData.append("files", file, file.name);

      try {
        const response = await options.fetchImpl(options.action, {
          method: "POST",
          credentials: "same-origin",
          redirect: "follow",
          body: formData,
        });
        const responsePath = new URL(response.url || options.action, options.action).pathname;
        if (!response.ok || responsePath !== "/") {
          return {
            complete: false,
            acceptedFiles,
            failedChunk: index + 1,
            totalChunks: chunks.length,
            unknownOutcome: response.status >= 500 || responsePath === "/login",
            httpStatus: response.status,
          };
        }
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
    return { complete: true, acceptedFiles, totalChunks: chunks.length, unknownOutcome: false };
  }

  const api = { chunkFiles, validateFiles, chunkIdempotencyKey, filesFingerprint, resolveBatchKey, clearBatchKey, uploadChunks };
  if (typeof module !== "undefined" && module.exports) module.exports = api;

  if (typeof document === "undefined") return;
  const form = document.querySelector("#document-upload");
  if (!form) return;
  const fileInput = form.querySelector('input[type="file"]');
  const button = form.querySelector('button[type="submit"]');
  const status = document.querySelector("#document-upload-status");
  const generatedBaseKey = form.querySelector('input[name="idempotency_key"]').value;
  // Keep the storage slot name so pending v1 keys survive a code deployment.
  const storageKey = "sro-upload-batch-v1";
  let batchStorage;
  let legacyBatchStorage;
  try {
    batchStorage = root.localStorage;
  } catch (_error) {
    batchStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
  }
  try {
    legacyBatchStorage = root.sessionStorage;
  } catch (_error) {
    legacyBatchStorage = null;
  }
  let baseKey = generatedBaseKey;
  let busy = false;

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy) return;
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

    busy = true;
    button.disabled = true;
    fileInput.disabled = true;
    button.textContent = "Загрузка…";
    status.textContent = `Отправлено 0 из ${files.length} файлов.`;
    let result;
    try {
      baseKey = await resolveBatchKey(files, generatedBaseKey, {
        cryptoApi: root.crypto,
        storage: batchStorage,
        legacyStorage: legacyBatchStorage,
        storageKey,
      });
      result = await uploadChunks({
        action: form.action,
        baseKey,
        files,
        filesPerRequest: Number(form.dataset.filesPerRequest),
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
        unknownOutcome: false,
      };
    }

    busy = false;
    if (result.complete) {
      clearBatchKey({ storage: batchStorage, legacyStorage: legacyBatchStorage, storageKey }, baseKey);
      button.textContent = "Принято";
      status.textContent = `Принято ${result.acceptedFiles} файлов. Открываю список карточек для проверки…`;
      root.setTimeout(() => root.location.assign("/"), 500);
      return;
    }

    button.disabled = false;
    fileInput.disabled = false;
    if (!result.unknownOutcome && result.httpStatus >= 400 && result.httpStatus < 500) {
      clearBatchKey({ storage: batchStorage, legacyStorage: legacyBatchStorage, storageKey }, baseKey);
      button.textContent = "Загрузить исправленные файлы";
      status.textContent = `Сервер отклонил часть ${result.failedChunk} из ${result.totalChunks} (HTTP ${result.httpStatus}). Исправьте выбор и отправьте снова; подтверждённые ${result.acceptedFiles} файлов уже приняты.`;
      return;
    }
    button.textContent = "Повторить с теми же файлами";
    const outcome = result.unknownOutcome ? "Ответ сервера не подтверждён" : "Сервер отклонил часть загрузки";
    status.textContent = `${outcome}. Подтверждено ${result.acceptedFiles} из ${files.length} файлов; часть ${result.failedChunk} из ${result.totalChunks}. Повтор безопасно использует те же ключи для уже принятых частей.`;
  });

})(typeof globalThis === "undefined" ? window : globalThis);
