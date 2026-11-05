(function (root) {
  "use strict";

  const GROUP_LABELS = {
    persons: "Физические лица",
    organizations: "Юридические лица и ИП",
    assignments: "Назначения этой партии",
    review: "Нужна проверка",
    pending: "Ожидают распознавания",
  };
  const STATUS_LABELS = {
    new: "В очереди OCR",
    recognizing: "Распознаётся",
    needs_review: "Нужна проверка данных",
    approved: "Данные проверены",
    writing: "Создаётся техническая карточка СРО и заполняется фотографиями",
    awaiting_preview: "Фотографии заполнены; карточка готова к проверке",
    add_started: "Попытка создания технической карточки началась; результат ещё не установлен",
    add_bound: "Техническая карточка создана; фотографии ожидают проверки",
    add_unknown: "Результат создания карточки неизвестен; повтор заблокирован до сверки",
    technical_card_pending: "Ожидает OCR и подготовки карточки",
    technical_card_preparing: "Подготовка технической карточки выполняется",
    technical_card_preview_ready: "Фотографии заполнены; карточка готова к проверке",
    technical_card_add_unknown: "Результат создания карточки неизвестен; нужна сверка в СРО перед повтором",
    technical_card_failed: "Подготовка технической карточки завершилась ошибкой",
    failed: "Ошибка обработки",
    pending_ocr: "Ожидает OCR",
    admitted: "Подготовка формы поставлена в очередь",
    withheld: "Подготовка формы не разрешена текущими данными",
    existing: "Запрос подготовки уже зарегистрирован",
    missing_task: "Задание не найдено",
    queue_uncertain: "Передача команды в очередь не подтверждена. Проверьте задание и СРО перед любым повтором",
    stale: "Данные изменились; обновите состояние перед повтором",
    save_requested: "Команда сохранения принята; ожидается подтверждение СРОАрбитра",
    save_started: "СРОАрбитр принял команду; ожидается сверка результата",
    saved: "Сохранение в СРОАрбитре подтверждено",
    save_unknown: "Исход сохранения неизвестен. Сверьте СРОАрбитр перед любым повтором",
    local_finalization_pending: "внутреннее оформление ожидает завершения",
    local_finalization_failed: "внутреннее оформление завершилось ошибкой",
    local_finalization_succeeded: "внутреннее оформление завершено",
    not_ready: "Сохранение не отправлено: форма больше не готова",
    stale_or_unavailable: "Сохранение не подтверждено; сверьте состояние задания перед повтором",
    state_uncertain: "Не удалось подтвердить локальное состояние запроса. Сверьте задание и СРО перед любым повтором",
    prepare_status_pending_ocr: "Подготовка формы ждёт OCR",
    prepare_status_admitted: "Создание технической карточки поставлено в очередь",
    prepare_status_withheld: "Создание карточки удержано для проверки",
    prepare_status_queue_uncertain: "Передача подготовки не подтверждена; проверьте состояние задания",
    assignment_status_draft: "Черновик назначения создан",
    assignment_status_awaiting_information: "Назначение ждёт сведений",
    assignment_status_ready_for_review: "Назначение готово к проверке",
  };
  const REASON_LABELS = {
    ocr_pending: "Ждём завершения распознавания.",
    ocr_failed: "Распознавание завершилось ошибкой. Откройте задание, чтобы проверить причину.",
    explicit_applicant: "Черновик по заявителю. Выберите подтверждённую карточку должника и проверьте сведения об управляющем.",
    participant_role_uncertain: "Роль участника не подтверждена. Проверьте исходный документ и распознанные данные.",
    explicit_person_debtor: "Тип карточки определён для маршрутизации. Проверьте предложенные поля по оригиналу и снимок формы.",
    explicit_entity_debtor: "Тип карточки определён для маршрутизации. Проверьте предложенные поля по оригиналу и снимок формы.",
    review_person_candidate: "ФИО найдено, но роль должника требует проверки.",
    review_entity_candidate: "Найдены данные организации, но роль должника требует проверки.",
  };

  function randomRequestKey(cryptoApi) {
    if (!cryptoApi || typeof cryptoApi.getRandomValues !== "function") {
      throw new Error("Не удалось создать ключ запроса.");
    }
    return Array.from(cryptoApi.getRandomValues(new Uint8Array(24)), (byte) => byte.toString(16).padStart(2, "0")).join("");
  }

  async function readJSON(response) {
    let body;
    try { body = await response.json(); } catch (_error) { body = null; }
    if (!response.ok || !body || typeof body !== "object") {
      const detail = body && typeof body.detail === "string" ? body.detail : `HTTP ${response.status}`;
      throw new Error(detail);
    }
    return body;
  }

  async function getState(url, fetchImpl) {
    return readJSON(await fetchImpl(url, {
      method: "GET", credentials: "same-origin", headers: { Accept: "application/json" },
    }));
  }

  async function postAction(url, payload, fetchImpl) {
    return readJSON(await fetchImpl(url, {
      method: "POST", credentials: "same-origin",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }));
  }

  function text(node, value) { node.textContent = value == null ? "" : String(value); return node; }

  function link(documentApi, href, label) {
    if (!href) return null;
    const anchor = documentApi.createElement("a");
    anchor.href = href;
    anchor.textContent = label;
    return anchor;
  }

  function appendSeparated(parent, node) {
    if (!node) return;
    if (parent.childNodes.length) parent.append(parent.ownerDocument.createTextNode(" · "));
    parent.append(node);
  }

  function statusText(row) {
    if (row && row.status && STATUS_LABELS[row.status]) return STATUS_LABELS[row.status];
    return row && row.status ? "Состояние обработки уточняется" : "Файл пока не принят сервером";
  }

  function recognitionStatusText(row) {
    const labels = {
      new: "Ожидает распознавания",
      recognizing: "Распознаётся",
      needs_review: "Распознано; поля требуют проверки",
      approved: "Данные проверены",
      writing: "Распознано; поля требуют проверки",
      awaiting_preview: "Распознано; поля требуют проверки",
      failed: "Ошибка распознавания",
    };
    return row && labels[row.status] || "Состояние распознавания уточняется";
  }

  function shouldShowPrepare(row) {
    return Boolean(row && row.can_prepare && row.technical_card_state !== "add_unknown"
      && row.prepare_status !== "admitted");
  }

  function prepareButtonLabel(row) {
    return row && row.prepare_status === "queue_uncertain"
      ? "Повторить передачу подготовки" : "Создать карточку в СРО";
  }

  function assignmentStatusText(row) {
    if (!row || row.reason !== "explicit_applicant" || !row.assignment_url) return "";
    if (row.assignment_needs_debtor || row.assignment_needs_practitioner) {
      const missing = [
        row.assignment_needs_debtor ? "нужно выбрать карточку должника" : "",
        row.assignment_needs_practitioner ? "нужны сведения об управляющем" : "",
      ].filter(Boolean).join("; ");
      return `Черновик назначения: ${missing}`;
    }
    return STATUS_LABELS[`assignment_status_${row.assignment_status}`] || "Черновик назначения создан";
  }

  function render(rootNode, state, handlers) {
    const documentApi = rootNode.ownerDocument;
    const content = rootNode.querySelector("[data-mixed-content]");
    const progress = rootNode.querySelector("[data-mixed-progress]");
    if (!content || !state || !state.groups) return;
    const selected = handlers.selected;
    const expanded = new Set([...content.querySelectorAll(".mixed-batch-row")]
      .filter((row) => row.querySelector("details")?.open)
      .map((row) => Number(row.dataset.fileIndex)));
    content.replaceChildren();

    const total = Number.isSafeInteger(state.total) ? state.total : 0;
    let processed = 0;
    let withPreview = 0;
    let failed = 0;
    for (const rows of Object.values(state.groups)) {
      for (const row of rows || []) {
        if (["awaiting_preview"].includes(row.status)) withPreview += 1;
        else if (row.status === "failed") failed += 1;
        else if (row.task_id != null && row.status && !["new", "recognizing"].includes(row.status)) processed += 1;
      }
    }
    const recognitionOnly = state.recognition_only === true || rootNode.dataset.recognitionOnly === "true";
    text(progress, recognitionOnly
      ? `Файлов: ${total} · распознано: ${processed} · ошибки OCR: ${failed}`
      : `Файлов: ${total} · обработаны OCR: ${processed} · фотографии готовы к проверке: ${withPreview} · ошибки OCR: ${failed}`);

    if (!recognitionOnly) {
      const toolbar = documentApi.createElement("div");
      toolbar.className = "mixed-batch-toolbar";
      const selectionLabel = documentApi.createElement("span");
      selectionLabel.dataset.mixedSelectionCount = "";
      const saveSelected = documentApi.createElement("button");
      saveSelected.type = "button";
      saveSelected.className = "btn-primary";
      saveSelected.dataset.mixedSaveSelected = "";
      saveSelected.addEventListener("click", () => handlers.save([...selected]));
      toolbar.append(selectionLabel, saveSelected);
      content.append(toolbar);
    }

    const groups = documentApi.createElement("div");
    groups.className = "mixed-batch-groups";
    for (const [groupName, rows] of Object.entries(state.groups)) {
      const section = documentApi.createElement("section");
      section.className = "mixed-batch-group";
      const heading = documentApi.createElement("h3");
      text(heading, `${GROUP_LABELS[groupName] || groupName} (${(rows || []).length})`);
      const folderUrlBase = rootNode.dataset.folderUrlBase;
      if (folderUrlBase && ["persons", "organizations", "assignments"].includes(groupName)
        && groupName !== rootNode.dataset.selectedRegistry) {
        heading.append(documentApi.createTextNode(" · "));
        heading.append(link(documentApi, `${folderUrlBase}${groupName}`, "Открыть папку"));
      }
      section.append(heading);
      const list = documentApi.createElement("div");
      list.className = "mixed-batch-rows";
      for (const row of rows || []) {
        const article = renderRow(documentApi, row, selected, { ...handlers, recognitionOnly });
        const details = article.querySelector("details");
        if (details && expanded.has(row.file_index)) details.open = true;
        list.append(article);
      }
      section.append(list);
      groups.append(section);
    }
    content.append(groups);
    if (!recognitionOnly) updateSelection(rootNode, selected);
    if (handlers.results) {
      for (const [index, message] of handlers.results) {
        const line = rootNode.querySelector(`[data-mixed-result="${index}"]`);
        if (line) text(line, message);
      }
    }
  }

  function renderRow(documentApi, row, selected, handlers) {
    const article = documentApi.createElement("article");
    article.className = "mixed-batch-row";
    article.dataset.fileIndex = String(row.file_index);
    const head = documentApi.createElement("div");
    head.className = "mixed-batch-row-head";
    const title = documentApi.createElement("div");
    title.className = "mixed-batch-row-title";
    if (!handlers.recognitionOnly && row.can_save && row.preview_token && !handlers.blocked.has(row.file_index)) {
      const label = documentApi.createElement("label");
      label.className = "mixed-batch-selection";
      const checkbox = documentApi.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = selected.has(row.file_index);
      checkbox.setAttribute("aria-label", `Выбрать ${row.filename} для сохранения в СРО`);
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) selected.add(row.file_index);
        else selected.delete(row.file_index);
        updateSelection(handlers.root, selected);
      });
      label.append(checkbox);
      title.append(label);
    }
    const strong = documentApi.createElement("strong");
    text(strong, `${Number(row.file_index) + 1}. ${row.filename || "Без имени"}`);
    title.append(strong);
    const state = documentApi.createElement("span");
    state.className = "mixed-batch-state";
    const isApplicant = row.reason === "explicit_applicant";
    const recognitionOnly = handlers.recognitionOnly;
    const technicalState = !recognitionOnly && !isApplicant && row.technical_card_state
      ? STATUS_LABELS[`technical_card_${row.technical_card_state}`] || "Состояние технической карточки требует проверки"
      : !recognitionOnly && !isApplicant && row.owned_add_state && row.owned_add_state !== "not_started"
        ? STATUS_LABELS[`add_${row.owned_add_state}`] || "Состояние технической карточки требует проверки" : "";
    const assignmentState = assignmentStatusText(row);
    const queuedBeforeAdd = row.prepare_status === "admitted" && row.owned_add_state === "not_started";
    const workflowState = recognitionOnly
      ? assignmentState || recognitionStatusText(row)
      : assignmentState || (queuedBeforeAdd
      ? "Подготовка технической карточки ожидает начала"
      : technicalState || statusText(row));
    const prepareStatus = !recognitionOnly && !isApplicant && row.prepare_status && row.prepare_status !== "not_requested"
      && row.prepare_status !== "admitted"
      ? ` · ${STATUS_LABELS[`prepare_status_${row.prepare_status}`] || "Состояние подготовки уточняется"}` : "";
    const localStatus = !recognitionOnly && !isApplicant && row.local_finalization_state && row.local_finalization_state !== "not_applicable"
      ? ` · ${STATUS_LABELS[`local_finalization_${row.local_finalization_state}`] || "Внутреннее оформление требует проверки"}` : "";
    const refreshStatus = !recognitionOnly && !isApplicant && row.preview_refresh_state && row.preview_refresh_state !== "idle"
      ? ` · ${({ requested: "Запрошено обновление снимков", started: "Снимки обновляются", unknown: "Исход обновления снимков неизвестен; проверьте задание" })[row.preview_refresh_state] || "Обновление снимков требует проверки"}` : "";
    text(state, `${workflowState}${prepareStatus}${localStatus}${refreshStatus}`);
    head.append(title, state);
    article.append(head);

    const actions = documentApi.createElement("div");
    actions.className = "mixed-batch-actions";
    appendSeparated(actions, link(documentApi, row.source_url, "Исходный файл"));
    appendSeparated(actions, link(documentApi, row.preview_url || row.task_url, "Проверить задание"));
    appendSeparated(actions, link(documentApi, row.assignment_url, "Открыть черновик назначения"));
    if (!recognitionOnly && shouldShowPrepare(row)) {
      const prepare = documentApi.createElement("button");
      prepare.type = "button";
      prepare.className = "btn-secondary";
      prepare.textContent = prepareButtonLabel(row);
      prepare.addEventListener("click", () => handlers.prepare([row.file_index]));
      appendSeparated(actions, prepare);
    } else if (!recognitionOnly && row.can_save && row.preview_token && !handlers.blocked.has(row.file_index)) {
      const save = documentApi.createElement("button");
      save.type = "button";
      save.className = "btn-primary";
      save.textContent = row.save_retry ? "Повторить отправку команды" : "Сохранить в СРО";
      save.addEventListener("click", () => handlers.save([row.file_index]));
      appendSeparated(actions, save);
    } else if (row.save_block_reason && row.status === "awaiting_preview") {
      const reason = documentApi.createElement("span");
      reason.className = "field-warning";
      text(reason, row.save_block_reason);
      appendSeparated(actions, reason);
    }
    if (!recognitionOnly && row.technical_card_state === "add_unknown") {
      const held = documentApi.createElement("span");
      held.className = "field-warning";
      text(held, "Нужна ручная сверка результата Add в СРО; повтор подготовки недоступен.");
      appendSeparated(actions, held);
    }
    if (!recognitionOnly && row.can_reconcile_local) {
      const reconcile = documentApi.createElement("button");
      reconcile.type = "button";
      reconcile.className = "btn-secondary";
      reconcile.textContent = "Повторить только внутреннее оформление";
      reconcile.addEventListener("click", () => handlers.reconcile(row.file_index));
      appendSeparated(actions, reconcile);
      const externalSaved = documentApi.createElement("p");
      externalSaved.className = "muted";
      externalSaved.textContent = "Сохранение в СРОАрбитре уже подтверждено; повторяется только внутреннее оформление.";
      article.append(externalSaved);
    }
    article.append(actions);

    if (row.reason && !(isApplicant && row.assignment_url)) {
      const reason = documentApi.createElement("p");
      reason.className = "muted";
      const recognitionReason = recognitionOnly && ["explicit_person_debtor", "explicit_entity_debtor"].includes(row.reason)
        ? "Тип карточки определён для распределения. Сверьте предложенные поля с оригиналом."
        : REASON_LABELS[row.reason] || "Требуется проверить документ и предложенные данные.";
      text(reason, recognitionReason);
      article.append(reason);
    }
    if (row.role_review_required) {
      const roleWarning = documentApi.createElement("p");
      roleWarning.className = "field-warning";
      roleWarning.setAttribute("role", "note");
      text(roleWarning, "Сверьте предложенную роль по оригиналу. Автоматическое распределение не заменяет проверку сотрудника.");
      article.append(roleWarning);
    }
    if (!recognitionOnly && !isApplicant && row.prepare_block_reason && !row.can_prepare && row.status !== "awaiting_preview") {
      const blocked = documentApi.createElement("p");
      blocked.className = "muted";
      text(blocked, row.prepare_block_reason);
      article.append(blocked);
    }

    const details = documentApi.createElement("details");
    const summary = documentApi.createElement("summary");
    summary.textContent = "Источник, предложенные данные и снимки формы";
    details.append(summary);
    const sourcePreviewUrl = row.source_preview_url;
    const suffix = String(row.source_kind || row.filename || "").toLowerCase().split(".").pop();
    if (sourcePreviewUrl && suffix === "pdf") {
      const frame = documentApi.createElement("iframe");
      frame.className = "mixed-batch-source-preview";
      frame.src = sourcePreviewUrl;
      frame.title = `Исходный документ ${row.filename || ""}`;
      frame.loading = "lazy";
      details.append(frame);
    } else if (sourcePreviewUrl && ["jpg", "jpeg", "png", "webp", "bmp", "heic"].includes(suffix)) {
      const image = documentApi.createElement("img");
      image.src = sourcePreviewUrl;
      image.alt = `Предпросмотр источника: ${row.filename || ""}`;
      image.loading = "lazy";
      image.style.maxWidth = "100%";
      details.append(image);
    } else if (sourcePreviewUrl) {
      appendSeparated(details, link(documentApi, sourcePreviewUrl, "Открыть предпросмотр источника"));
    }
    if (row.data_summary) {
      const dataNote = documentApi.createElement("strong");
      dataNote.className = "field-warning";
      text(dataNote, "Проверьте каждое предложенное поле по оригиналу.");
      details.append(dataNote);
      const data = documentApi.createElement("p");
      data.className = "mixed-batch-data";
      text(data, row.data_summary);
      details.append(data);
    }
    if (!recognitionOnly && Array.isArray(row.screenshots) && row.screenshots.length) {
      const shots = documentApi.createElement("div");
      shots.className = "mixed-batch-shots";
      for (const shot of row.screenshots) {
        if (!shot || !shot.url) continue;
        const anchor = link(documentApi, shot.url, "");
        const image = documentApi.createElement("img");
        image.src = shot.url;
        image.alt = shot.alt || "Снимок заполненной формы СРО";
        image.loading = "lazy";
        anchor.append(image);
        shots.append(anchor);
      }
      if (shots.childNodes.length) details.append(shots);
    } else if (!recognitionOnly && (row.status === "awaiting_preview" || row.status === "writing")) {
      const waiting = documentApi.createElement("p");
      waiting.className = "muted";
      waiting.textContent = "Снимки формы ещё не сохранены.";
      details.append(waiting);
    }
    article.append(details);
    const resultLine = documentApi.createElement("p");
    resultLine.className = "mixed-batch-result";
    resultLine.dataset.mixedResult = String(row.file_index);
    article.append(resultLine);
    return article;
  }

  function updateSelection(rootNode, selected) {
    const count = selected.size;
    const countNode = rootNode.querySelector("[data-mixed-selection-count]");
    const button = rootNode.querySelector("[data-mixed-save-selected]");
    if (countNode) text(countNode, `Выбрано к сохранению в СРО: ${count}`);
    if (button) {
      button.disabled = count === 0;
      button.textContent = `Сохранить выбранные в СРО (${count})`;
    }
  }

  function resultMessage(status) {
    return STATUS_LABELS[status] || (status ? "Состояние действия уточняется; проверьте задание" : "Ответ не содержит итогового состояния");
  }

  function preparationSummary(results) {
    const counts = new Map();
    for (const result of results || []) counts.set(result.status, (counts.get(result.status) || 0) + 1);
    const parts = [];
    for (const [status, label] of [
      ["admitted", "создание карточки поставлено в очередь"],
      ["pending_ocr", "ждут OCR до создания карточки"],
      ["withheld", "удержано для проверки"],
      ["existing", "запрос создания уже зарегистрирован"],
      ["queue_uncertain", "передача в очередь не подтверждена"],
      ["missing_task", "без задания"],
    ]) {
      const count = counts.get(status) || 0;
      if (count) parts.push(`${label}: ${count}`);
    }
    return parts.length ? parts.join(" · ") : "сервер не вернул состояния по отдельным файлам";
  }

  function saveConfirmation(count) {
    return `Сохранить в СРО выбранные карточки: ${count}. Вы подтверждаете, что проверили предложенную роль, поля и снимки?`;
  }

  function blocksSaveRetry(status) {
    return ["state_uncertain", "queue_uncertain", "stale_or_unavailable", "save_started", "saved", "save_unknown"]
      .includes(status);
  }

  function initUpload(documentApi, fetchImpl, cryptoApi) {
    const form = documentApi.querySelector("form#document-upload-form[data-mixed-batch='true']");
    if (!form) return;
    const status = documentApi.querySelector("#document-upload-status");
    root.addEventListener("sro:mixed-upload-accepted", async (event) => {
      const batchId = event.detail && event.detail.batch_id;
      if (!/^[0-9a-f]{32}$/.test(batchId || "")) {
        if (status) status.textContent = "Партия принята, но её идентификатор не подтверждён. Проверьте общий список партий.";
        return;
      }
      if (form.dataset.recognitionOnly === "true") {
        const workLink = link(documentApi, `/upload-batches/${batchId}/work`, "Открыть распознанные документы");
        if (status) status.replaceChildren(documentApi.createTextNode("Файлы приняты. Локальное распознавание и распределение продолжаются; подготовка карточек СРО отключена. "), workLink);
        return;
      }
      if (status) status.textContent = "Файлы приняты. Для поддерживаемых типов карточек подготовка создаёт технические карточки СРО и заполняет их фотографиями; распознавание продолжается параллельно…";
      const apiUrl = `/upload-batches/${batchId}/mixed`;
      const prepareUrl = `/upload-batches/${batchId}/mixed/prepare`;
      try {
        const state = await getState(apiUrl, fetchImpl);
        const result = await postAction(prepareUrl, {
          file_indices: Array.from({ length: state.total }, (_item, index) => index),
          snapshot_token: state.snapshot_token,
          request_key: randomRequestKey(cryptoApi),
        }, fetchImpl);
        const linkNode = link(documentApi, `/upload-batches/${batchId}/work`, "Открыть распределение и проверку");
        if (status) {
          status.replaceChildren(documentApi.createTextNode(`Состояние подготовки: ${preparationSummary(result.results)}. Команда «Сохранить в СРО» ещё не отправлялась. `), linkNode);
        }
      } catch (error) {
        if (status) status.textContent = `Файлы приняты, но запуск подготовки не подтверждён: ${error.message}. Откройте партию и проверьте её состояние.`;
      }
    });
  }

  function initWork(documentApi, fetchImpl, cryptoApi) {
    const rootNode = documentApi.querySelector("#mixed-batch[data-api-url]");
    if (!rootNode) return;
    const selected = new Set();
    let latestState = null;
    let busy = false;
    const handlers = {
      root: rootNode,
      selected,
      results: new Map(),
      blocked: new Set(),
      save: async (indices) => {
        const rows = indices.map((index) => Object.values(latestState.groups).flat().find((row) => row.file_index === index));
        const eligible = rows.filter((row) => row && row.can_save && row.preview_token && !handlers.blocked.has(row.file_index));
        if (!eligible.length || busy) return;
        const count = eligible.length;
        if (!root.confirm(saveConfirmation(count))) return;
        busy = true;
        const results = await runAction(rootNode, "save", {
          items: eligible.map((row) => ({ file_index: row.file_index, preview_token: row.preview_token })),
          snapshot_token: latestState.snapshot_token,
          request_key: randomRequestKey(cryptoApi),
        }, fetchImpl, selected);
        busy = false;
        showResults(rootNode, results, handlers);
        await refresh();
      },
      prepare: async (indices) => {
        const rows = indices.map((index) => Object.values(latestState.groups).flat().find((row) => row.file_index === index));
        const eligible = rows.filter((row) => row && row.can_prepare);
        if (!eligible.length || busy) return;
        busy = true;
        const results = await runAction(rootNode, "prepare", {
          file_indices: eligible.map((row) => row.file_index),
          snapshot_token: latestState.snapshot_token,
          row_tokens: Object.fromEntries(eligible.map((row) => [row.file_index, row.row_token])),
          request_key: randomRequestKey(cryptoApi),
        }, fetchImpl, selected);
        busy = false;
        showResults(rootNode, results, handlers);
        await refresh();
      },
      reconcile: async (fileIndex) => {
        if (busy || !latestState) return;
        busy = true;
        try {
          const result = await postAction(rootNode.dataset.reconcileUrl, {
            file_index: fileIndex,
            snapshot_token: latestState.snapshot_token,
            request_key: randomRequestKey(cryptoApi),
          }, fetchImpl);
          const statusKey = result.status === "succeeded" ? "local_finalization_succeeded"
            : result.status === "failed" ? "local_finalization_failed"
              : result.status === "pending" ? "local_finalization_pending" : result.status;
          showResults(rootNode, [{ ...result, status: statusKey }], handlers);
        } catch (error) {
          showResults(rootNode, [{ file_index: fileIndex, status: `Ошибка локального повтора: ${error.message}` }], handlers);
        }
        busy = false;
        await refresh();
      },
    };
    async function refresh() {
      try {
        latestState = await getState(rootNode.dataset.apiUrl, fetchImpl);
        for (const index of [...selected]) {
          const row = Object.values(latestState.groups).flat().find((item) => item.file_index === index);
          if (!row || !row.can_save || !row.preview_token) selected.delete(index);
        }
        render(rootNode, latestState, handlers);
      } catch (error) {
        const progress = rootNode.querySelector("[data-mixed-progress]");
        if (progress) text(progress, `Не удалось обновить состояние партии: ${error.message}`);
      }
    }
    rootNode.refreshMixedBatch = refresh;
    refresh();
    root.setInterval(() => { if (!documentApi.hidden) refresh(); }, 3000);
  }

  async function runAction(rootNode, action, payload, fetchImpl) {
    const url = action === "save" ? rootNode.dataset.saveUrl : rootNode.dataset.prepareUrl;
    const localResult = [];
    try {
      const response = await postAction(url, payload, fetchImpl);
      return Array.isArray(response.results) ? response.results : localResult;
    } catch (error) {
      return payload.items
        ? payload.items.map((item) => ({ file_index: item.file_index, status: `Ошибка запроса: ${error.message}` }))
        : payload.file_indices.map((file_index) => ({ file_index, status: `Ошибка запроса: ${error.message}` }));
    }
  }

  function showResults(rootNode, results, handlers) {
    for (const result of results || []) {
      const line = rootNode.querySelector(`[data-mixed-result="${Number(result.file_index)}"]`);
      const message = resultMessage(result.status);
      if (line) text(line, message);
      if (handlers) {
        if (handlers.results) handlers.results.set(Number(result.file_index), message);
        if (blocksSaveRetry(result.status)) {
          handlers.blocked.add(Number(result.file_index));
          const row = rootNode.querySelector(`[data-file-index="${Number(result.file_index)}"]`);
          const checkbox = row && row.querySelector("input[type='checkbox']");
          if (checkbox) checkbox.checked = false;
          if (handlers.selected) handlers.selected.delete(Number(result.file_index));
        }
      }
    }
  }

  const api = { randomRequestKey, readJSON, getState, postAction, render, statusText, recognitionStatusText, shouldShowPrepare, prepareButtonLabel, assignmentStatusText, resultMessage, preparationSummary, saveConfirmation, blocksSaveRetry };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (!root.document) return;
  initUpload(root.document, root.fetch.bind(root), root.crypto);
  initWork(root.document, root.fetch.bind(root), root.crypto);
}(typeof window !== "undefined" ? window : globalThis));
