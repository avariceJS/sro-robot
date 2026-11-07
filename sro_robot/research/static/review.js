/* Local owner feedback. Source values never become HTML or independent gold. */
(function (root) {
  "use strict";
  const FIELDS = [
    ["fio_raw", "ФИО участника"], ["inn", "ИНН"], ["snils", "СНИЛС"],
    ["address", "Полный адрес, включая квартиру"], ["address_kind", "Тип адреса"],
    ["case_number", "Номер дела"], ["doc_date", "Дата судебного акта"], ["court", "Суд"],
    ["entity_kind", "Тип участника"], ["legal_name", "Полное наименование организации"],
    ["short_name", "Краткое наименование"], ["legal_form", "Организационно-правовая форма"],
    ["debtor_name", "Наименование должника"], ["ogrn", "ОГРН"], ["ogrnip", "ОГРНИП"],
    ["kpp", "КПП"], ["doc_type", "Вид судебного акта"]
  ];
  const STATUS_KEYS = ["downloading", "processing", "pending_review", "approved", "rejected", "error", "total"];
  const PENDING_KEY = "sro-local-review-pending-v1";
  const stringValue = (field) => {
    const value = field && typeof field === "object" ? field.value : field;
    return value == null ? "" : String(value);
  };
  function correctedFields(original, values) {
    const result = {};
    for (const [key] of FIELDS) {
      const value = String(values[key] == null ? "" : values[key]);
      if (value !== stringValue(original[key])) result[key] = value;
    }
    return result;
  }
  function makePayload(doc, values, incorrect, comment, decision, actionId) {
    if (!doc || !["approved", "rejected"].includes(decision)) throw new Error("invalid_review");
    if (!/^[0-9a-f]{64}$/.test(doc.snapshot_sha256 || "")) throw new Error("review_version_unavailable");
    const corrections = correctedFields(doc.original_fields || doc.fields || {}, values);
    const names = FIELDS.map(([key]) => key).filter((key) => incorrect.includes(key) || key in corrections);
    if (decision === "approved" && (names.length || String(comment).trim())) throw new Error("edited_card_requires_rejection");
    if (decision === "rejected" && !names.length && !String(comment).trim()) throw new Error("rejection_reason_required");
    return { action_id: actionId, expected_revision: doc.revision, expected_snapshot_sha256: doc.snapshot_sha256, decision,
      corrected_fields: corrections, incorrect_fields: names, comment: String(comment) };
  }
  function shortcut(event) {
    if (event.isComposing || event.repeat || event.defaultPrevented) return null;
    if ((event.ctrlKey || event.metaKey) && !event.altKey && event.key === "Enter") return event.shiftKey ? "reject" : "approve";
    if (event.altKey && !event.ctrlKey && !event.metaKey && !event.shiftKey && event.key === "ArrowRight") return "skip";
    return null;
  }
  function safeOriginalURL(value, docId, base) {
    const fallback = "/api/documents/" + encodeURIComponent(docId) + "/original";
    try {
      const origin = new URL(base);
      const url = new URL(value || fallback, origin);
      if (url.origin === origin.origin && url.pathname.startsWith("/api/documents/") && !url.username && !url.password) return url.pathname + url.search;
    } catch (_) { /* Fixed local fallback only. */ }
    return fallback;
  }
  function validPending(value) {
    return value && typeof value === "object" && typeof value.documentId === "string"
      && value.payload && typeof value.payload.action_id === "string"
      && ["approved", "rejected"].includes(value.payload.decision)
      && value.payload.corrected_fields && typeof value.payload.corrected_fields === "object"
      && Array.isArray(value.payload.incorrect_fields);
  }
  const exports = { FIELDS, STATUS_KEYS, stringValue, correctedFields, makePayload, shortcut, safeOriginalURL, validPending };
  if (typeof module !== "undefined" && module.exports) module.exports = exports;
  root.SROLocalReview = exports;
  if (!root.document) return;

  const document = root.document;
  const byId = (id) => document.getElementById(id);
  const state = { current: null, inputs: {}, checks: {}, rows: {}, busy: false, conflict: false,
    pending: null, page: 1, zoom: 100, originalURL: "", rejectionOpen: false, polling: false, loading: false, statsEpoch: 0 };
  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = String(text);
    return node;
  }
  function message(text, error = false) {
    byId("notice").textContent = text;
    byId("notice").hidden = !text;
    byId("notice").classList.toggle("error", error);
  }
  function connection(online) {
    byId("connection-dot").classList.toggle("online", online);
    byId("connection-label").textContent = online ? "Очередь обновляется автоматически" : "Нет связи с локальным сервером";
  }
  function updateStats(stats) {
    for (const key of STATUS_KEYS) {
      const value = Number(stats && stats[key]);
      byId("count-" + key).textContent = Number.isFinite(value) && value >= 0 ? String(value) : "—";
    }
  }
  async function request(path, options = {}) {
    let response;
    try { response = await root.fetch(path, { credentials: "same-origin", cache: "no-store", ...options }); }
    catch (_) { connection(false); throw new Error("network_unavailable"); }
    if (response.status === 401 || response.status === 403) throw new Error("session_unavailable");
    if (!response.ok) { const error = new Error(response.status === 409 ? "review_conflict" : "request_failed"); error.status = response.status; throw error; }
    connection(true);
    return response.json();
  }
  function formValues() { return Object.fromEntries(FIELDS.map(([key]) => [key, state.inputs[key] ? state.inputs[key].value : ""])); }
  function wrongFields() { return FIELDS.map(([key]) => key).filter((key) => state.checks[key] && state.checks[key].checked); }
  function hasEdits() { return state.current && (Object.keys(correctedFields(state.current.original_fields || state.current.fields || {}, formValues())).length > 0 || wrongFields().length > 0 || byId("review-comment").value.length > 0); }
  function updateButtons() {
    const enabled = Boolean(state.current) && !state.busy && !state.conflict && !state.pending;
    byId("approve").disabled = !enabled;
    byId("reject").disabled = !enabled;
    byId("skip").disabled = !enabled;
    byId("reject").textContent = state.rejectionOpen ? "Сохранить отклонение →" : "✕ Неправильно";
    for (const [key] of FIELDS) {
      if (state.inputs[key]) state.inputs[key].readOnly = state.busy || Boolean(state.pending) || state.conflict;
      if (state.checks[key]) state.checks[key].disabled = state.busy || Boolean(state.pending) || state.conflict;
    }
    byId("review-comment").readOnly = state.busy || Boolean(state.pending) || state.conflict;
  }
  function changed(key) {
    const edited = state.inputs[key].value !== stringValue((state.current.original_fields || state.current.fields || {})[key]);
    if (edited) state.checks[key].checked = true;
    state.rows[key].classList.toggle("is-edited", edited);
    state.rows[key].classList.toggle("is-incorrect", state.checks[key].checked);
    state.rows[key].querySelector(".original-value").hidden = !edited;
    if (state.checks[key].checked) openRejection(false);
  }
  function openRejection(focus = true) {
    state.rejectionOpen = true;
    byId("rejection-panel").hidden = false;
    updateButtons();
    if (focus) { byId("rejection-panel").scrollIntoView({ block: "nearest", behavior: "smooth" }); byId("review-comment").focus(); }
  }
  function participant(title, items, note) {
    const node = element("div", "participant-item"); node.append(element("strong", "", title));
    for (const text of items.filter(Boolean)) node.append(element("p", "", text));
    if (note) node.append(element("small", "", note));
    byId("participants").append(node);
  }
  function renderParticipants(doc) {
    byId("participants").replaceChildren();
    const observed = doc.observed_pipeline || {};
    const fields = doc.fields || {};
    const normalizedName = ["last_name", "first_name", "middle_name"].map((key) => stringValue(observed[key])).filter(Boolean).join(" ");
    const selected = stringValue(fields.fio_raw) || stringValue(fields.debtor_name) || stringValue(fields.legal_name) || normalizedName;
    const bound = [fields.fio_raw, fields.inn, fields.address].some((field) => field && field.source === "debtor_role:v1");
    participant("Основная карточка SRO", [selected || "Имя / наименование не выделено", bound ? "Роль: должник — по контексту SRO" : "Роль участника требует сверки с оригиналом"], "Проверьте, что реквизиты относятся к одному участнику.");
    if (observed.applicant_evidence && observed.applicant_evidence.name) participant("Заявитель — сведения из документа", [observed.applicant_evidence.name], "Это отдельный участник, его реквизиты нельзя смешивать с карточкой должника.");
    const participantLabels = { fio: "ФИО", inn: "ИНН", snils: "СНИЛС", address: "Адрес", birth_date: "Дата рождения", birth_place: "Место рождения", entity_kind: "Тип участника", legal_name: "Организация", debtor_name: "Наименование должника", ogrn: "ОГРН", ogrnip: "ОГРНИП", kpp: "КПП", short_name: "Краткое наименование", legal_form: "Организационно-правовая форма" };
    const candidates = Array.isArray(observed.review_suggestions) ? observed.review_suggestions : [];
    for (const candidate of candidates.slice(0, 30)) {
      const values = candidate.fields || {};
      const lines = Object.entries(values).filter(([key]) => key in participantLabels)
        .map(([key, field]) => participantLabels[key] + ": " + stringValue(field));
      participant("Кандидат: принадлежность не подтверждена", lines, candidate.warning || "Кандидат не является автоматически выбранным должником.");
    }
    if (observed.review_suggestions_complete === false || candidates.length > 30) participant("Список кандидатов неполный", ["Других участников и их реквизиты проверьте по оригиналу."], "Отсутствие кандидата в этом списке не доказывает отсутствие участника.");
  }
  function renderFields(doc) {
    byId("fields").replaceChildren(); state.inputs = {}; state.checks = {}; state.rows = {};
    for (const [key, title] of FIELDS) {
      const field = (doc.fields || {})[key] || {};
      const original = (doc.original_fields || doc.fields || {})[key];
      const row = element("div", "field-row"); const head = element("div", "field-head");
      const label = element("label", "field-label", title); label.htmlFor = "field-" + key;
      const wrong = element("label", "incorrect-toggle"); const check = element("input"); check.type = "checkbox"; check.setAttribute("aria-label", "Ошибка в поле: " + title); wrong.append(check, document.createTextNode("Ошибка"));
      head.append(label, wrong);
      const input = element("textarea"); input.id = "field-" + key; input.rows = key === "address" || key === "legal_name" ? 2 : 1; input.value = stringValue(field); input.placeholder = "SRO не выделил значение — это не доказанное отсутствие"; input.maxLength = 20000; input.autocomplete = "off"; input.spellcheck = false;
      const meta = element("div", "field-meta");
      if (field.source_page && Number.isInteger(Number(field.source_page))) { const page = element("button", "field-page", "Страница " + field.source_page); page.type = "button"; page.addEventListener("click", () => setPage(Number(field.source_page))); meta.append(page); }
      if (field.confidence != null && field.confidence !== "") meta.append(element("span", "", "Оценка SRO: " + field.confidence));
      const old = element("details", "original-value"); old.hidden = true; old.append(element("summary", "", "Исходный результат SRO"), element("p", "", stringValue(original) || "Не заполнено"));
      row.append(head, input, meta);
      if (field.warning) row.append(element("div", "field-warning", field.warning));
      row.append(old); byId("fields").append(row);
      state.inputs[key] = input; state.checks[key] = check; state.rows[key] = row;
      input.addEventListener("input", () => changed(key)); check.addEventListener("change", () => changed(key));
    }
  }
  function renderViewer() {
    const doc = state.current;
    const pdf = byId("document-pdf"), photo = byId("photo-viewport"), image = byId("document-photo");
    pdf.hidden = !doc || doc.media_type !== "application/pdf"; photo.hidden = !doc || doc.media_type === "application/pdf";
    byId("viewer-placeholder").hidden = Boolean(doc);
    byId("page-number").value = String(state.page); byId("page-total").textContent = "/ " + (doc ? Number(doc.page_count) || 1 : "—");
    byId("zoom-reset").textContent = state.zoom + "%";
    const pages = doc ? Math.max(1, Number(doc.page_count) || 1) : 1;
    byId("page-number").disabled = !doc || pages === 1; byId("previous-page").disabled = !doc || state.page <= 1; byId("next-page").disabled = !doc || state.page >= pages;
    for (const key of ["zoom-in", "zoom-out", "zoom-reset"]) byId(key).disabled = !doc;
    if (!doc) { pdf.removeAttribute("src"); image.removeAttribute("src"); return; }
    if (doc.media_type === "application/pdf") { image.removeAttribute("src"); pdf.src = state.originalURL + "#toolbar=1&navpanes=0&page=" + state.page + "&zoom=" + state.zoom; }
    else { pdf.removeAttribute("src"); const imageURL = state.originalURL.replace(/\/preview\/\d+$/, "/preview/" + state.page); if (image.getAttribute("src") !== imageURL) image.src = imageURL; image.style.width = state.zoom + "%"; }
  }
  function setPage(value) {
    if (!state.current) return;
    state.page = Math.max(1, Math.min(Number(state.current.page_count) || 1, Number(value) || 1)); renderViewer();
  }
  function setZoom(value) { state.zoom = Math.max(30, Math.min(400, value)); renderViewer(); }
  function renderDocument(doc) {
    state.current = doc; state.page = 1; state.zoom = 100; state.rejectionOpen = false; state.conflict = false;
    byId("rejection-panel").hidden = true; byId("review-comment").value = "";
    byId("conflict-actions").hidden = true; byId("retry-action").hidden = true;
    byId("review-content").scrollTop = 0; message("");
    byId("participant-context").hidden = !doc;
    if (doc) {
      renderFields(doc); renderParticipants(doc);
      state.originalURL = safeOriginalURL(doc.preview_url || doc.original_url, doc.id, root.location.href);
      byId("open-original").href = safeOriginalURL(doc.original_url, doc.id, root.location.href); byId("open-original").hidden = false;
      byId("document-id").textContent = "Документ " + String(doc.id).slice(0, 12);
      const identity = doc.parser_identity || {}; byId("document-version").textContent = "Парсер " + String(identity.revision || "версия сохранена").slice(0, 12) + (identity.source_dirty ? " · локальные изменения" : "");
    } else {
      byId("fields").replaceChildren(); state.inputs = {}; state.checks = {}; state.rows = {};
      byId("open-original").hidden = true; byId("document-id").textContent = "Очередь проверки пуста"; byId("document-version").textContent = "";
      byId("placeholder-title").textContent = "Всё проверено"; byId("placeholder-detail").textContent = "Новые документы появятся здесь автоматически после обработки.";
    }
    renderViewer(); updateButtons();
  }
  async function loadNext(after = "") {
    if (state.loading || state.busy || state.pending) return;
    state.loading = true;
    try { const response = await request("/api/next" + (after ? "?after=" + encodeURIComponent(after) : "")); updateStats(response.stats); renderDocument(response.document); }
    catch (error) { message(error.message === "session_unavailable" ? "Сессия недоступна. Откройте локальный адрес, показанный при запуске сервиса." : "Не удалось загрузить очередь. Повторное подключение выполняется автоматически.", true); }
    finally { state.loading = false; }
  }
  function savePending(value) {
    state.pending = value;
    try { if (value) root.sessionStorage.setItem(PENDING_KEY, JSON.stringify(value)); else root.sessionStorage.removeItem(PENDING_KEY); } catch (_) { /* Current in-memory retry stays valid. */ }
  }
  async function submitPending() {
    if (!state.pending || state.busy) return;
    const pending = state.pending; state.statsEpoch += 1; state.busy = true; updateButtons(); byId("retry-action").hidden = true; message("Сохраняем решение…");
    let advance = false;
    try {
      const response = await request("/api/documents/" + encodeURIComponent(pending.documentId) + "/review", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(pending.payload) });
      updateStats(response.stats); savePending(null); advance = true;
    } catch (error) {
      if (error.message === "review_conflict") {
        state.conflict = true; message("Карточка уже изменена в другой вкладке или версия исходного результата отличается. Ваши введённые значения сохранены здесь для сверки; они не перезаписали другую проверку и не применились к другому снимку.", true); byId("conflict-actions").hidden = false;
      } else if (error.status === 422) { message("Сервер не принял решение. Исправления сохранены в форме; проверьте отмеченные поля и комментарий.", true); savePending(null); }
      else { message(error.message === "session_unavailable" ? "Сессия недоступна. Решение сохранено в этой вкладке. Откройте адрес запуска сервиса, затем повторите сохранение." : "Ответ о сохранении не получен. Решение сохранено в этой вкладке; повторная отправка использует тот же идентификатор действия.", true); byId("retry-action").hidden = false; }
    } finally { state.busy = false; updateButtons(); }
    if (advance) { renderDocument(null); await loadNext(pending.documentId); }
  }
  async function decide(decision) {
    if (!state.current || state.busy || state.pending || state.conflict) return;
    if (decision === "rejected" && !state.rejectionOpen) { openRejection(); return; }
    let payload;
    try { payload = makePayload(state.current, formValues(), wrongFields(), byId("review-comment").value, decision, root.crypto.randomUUID()); }
    catch (error) {
      if (error.message === "review_version_unavailable") { message("Версия результата не подтверждена. Обновите страницу и заново сверьте документ перед сохранением.", true); }
      else if (error.message === "edited_card_requires_rejection") { openRejection(false); message("Есть исправления или отмеченные ошибки. Сохраните их кнопкой «Сохранить отклонение», чтобы исходная ошибка попала в разбор.", true); }
      else { openRejection(); message("Отметьте неправильное поле или добавьте комментарий о причине отклонения.", true); }
      return;
    }
    savePending({ documentId: state.current.id, payload }); await submitPending();
  }
  async function skip(force = false) {
    if (!state.current || state.busy || (state.pending && !force)) return;
    if (!force && hasEdits() && !root.confirm("Пропустить документ без сохранения введённых исправлений?")) return;
    const id = state.current.id; if (force) { savePending(null); state.conflict = false; }
    await loadNext(id);
  }
  async function poll() {
    if (state.polling || state.busy) return; state.polling = true; const epoch = state.statsEpoch;
    try { const stats = await request("/api/stats"); if (epoch !== state.statsEpoch) return; updateStats(stats); if (!state.current && !state.pending && !state.loading && Number(stats.pending_review) > 0) await loadNext(); }
    catch (_) { connection(false); }
    finally { state.polling = false; }
  }
  async function start() {
    byId("approve").addEventListener("click", () => decide("approved")); byId("reject").addEventListener("click", () => decide("rejected")); byId("skip").addEventListener("click", () => skip());
    byId("retry-action").addEventListener("click", submitPending); byId("conflict-next").addEventListener("click", () => skip(true));
    byId("previous-page").addEventListener("click", () => setPage(state.page - 1)); byId("next-page").addEventListener("click", () => setPage(state.page + 1)); byId("page-number").addEventListener("change", (event) => setPage(Number(event.target.value)));
    byId("zoom-in").addEventListener("click", () => setZoom(state.zoom + 25)); byId("zoom-out").addEventListener("click", () => setZoom(state.zoom - 25)); byId("zoom-reset").addEventListener("click", () => setZoom(100));
    byId("document-photo").addEventListener("error", () => message("Браузер не смог показать изображение. Откройте оригинал отдельно; данные формы сохранены.", true));
    document.addEventListener("keydown", (event) => { const action = shortcut(event); if (!action) return; event.preventDefault(); if (action === "approve") decide("approved"); if (action === "reject") decide("rejected"); if (action === "skip") skip(); });
    root.addEventListener("beforeunload", (event) => { if (state.pending || hasEdits()) { event.preventDefault(); event.returnValue = ""; } });
    const viewport = byId("photo-viewport"); let drag = null;
    viewport.addEventListener("pointerdown", (event) => { if (event.button !== 0) return; drag = { x: event.clientX, y: event.clientY, left: viewport.scrollLeft, top: viewport.scrollTop }; viewport.setPointerCapture(event.pointerId); viewport.classList.add("dragging"); });
    viewport.addEventListener("pointermove", (event) => { if (drag) { viewport.scrollLeft = drag.left - event.clientX + drag.x; viewport.scrollTop = drag.top - event.clientY + drag.y; } });
    const stopDrag = () => { drag = null; viewport.classList.remove("dragging"); }; viewport.addEventListener("pointerup", stopDrag); viewport.addEventListener("pointercancel", stopDrag);
    let pending = null;
    try { pending = JSON.parse(root.sessionStorage.getItem(PENDING_KEY) || "null"); } catch (_) { /* No draft. */ }
    if (validPending(pending)) {
      try {
        const response = await request("/api/documents/" + encodeURIComponent(pending.documentId)); renderDocument(response.document);
        for (const [key, value] of Object.entries(pending.payload.corrected_fields)) if (state.inputs[key]) { state.inputs[key].value = value == null ? "" : String(value); changed(key); }
        for (const key of pending.payload.incorrect_fields) if (state.checks[key]) { state.checks[key].checked = true; changed(key); }
        byId("review-comment").value = String(pending.payload.comment || ""); savePending(pending);
        if (!/^[0-9a-f]{64}$/.test(pending.payload.expected_snapshot_sha256 || "")) {
          state.conflict = true; updateButtons(); message("Сохранённое решение создано предыдущей версией интерфейса без привязки к снимку. Оно не отправлено: сверьте исходный документ заново. Введённые значения оставлены здесь для копирования.", true); byId("conflict-actions").hidden = false;
        } else await submitPending();
      } catch (_) { savePending(pending); message("Сохранённое решение ожидает связи с локальным сервером. Перезагрузите страницу после восстановления сервиса.", true); }
    } else await loadNext();
    root.setInterval(poll, 2000);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start, { once: true }); else start();
})(typeof window !== "undefined" ? window : globalThis);
