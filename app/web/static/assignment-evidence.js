"use strict";

const applicantPicker = document.getElementById("applicant-debtor-picker");
if (applicantPicker) {
  const query = document.getElementById("applicant-debtor-query");
  const searchButton = document.getElementById("applicant-debtor-search");
  const options = document.getElementById("applicant-debtor-options");
  const bindForm = document.getElementById("applicant-debtor-bind");
  const bindButton = document.getElementById("applicant-debtor-bind-button");
  const status = document.getElementById("applicant-debtor-search-status");
  let searchSequence = 0;

  function clearOptions(message) {
    options.replaceChildren(new Option(message, ""));
    options.disabled = true;
    bindButton.disabled = true;
  }

  async function searchCards() {
    const value = query.value.trim();
    const sequence = ++searchSequence;
    if (value.length < 2) {
      clearOptions("Введите не менее двух символов");
      status.textContent = "Укажите имя или ИНН для поиска среди проверенных карточек.";
      return;
    }
    searchButton.disabled = true;
    clearOptions("Ищем проверенные карточки…");
    status.textContent = "";
    try {
      const endpoint = new URL(applicantPicker.dataset.optionsUrl, window.location.origin);
      endpoint.searchParams.set("q", value);
      const response = await fetch(endpoint, {
        credentials: "same-origin",
        cache: "no-store",
        headers: { Accept: "application/json" },
      });
      let payload;
      try { payload = await response.json(); } catch { payload = null; }
      if (sequence !== searchSequence) return;
      if (!response.ok || !payload || !Array.isArray(payload.options)) {
        throw new Error(payload?.detail || `HTTP ${response.status}`);
      }
      options.replaceChildren(new Option(payload.options.length
        ? "Выберите проверенную карточку" : "Совпадений нет", ""));
      for (const card of payload.options) {
        const option = new Option(
          `${card.display_name}${card.inn ? ` · ИНН ${card.inn}` : ""}`,
          String(card.id),
        );
        option.dataset.entityKind = card.entity_kind || "";
        options.add(option);
      }
      options.disabled = payload.options.length === 0;
      status.textContent = payload.options.length
        ? `Найдено проверенных карточек: ${payload.options.length}. Выберите нужную карточку.`
        : "Подтверждённых совпадений нет. Черновик назначения не изменён.";
    } catch (error) {
      if (sequence !== searchSequence) return;
      clearOptions("Список недоступен");
      status.textContent = `Не удалось загрузить список проверенных карточек: ${error.message}`;
    } finally {
      if (sequence === searchSequence) searchButton.disabled = false;
    }
  }

  searchButton.addEventListener("click", searchCards);
  query.addEventListener("input", () => {
    searchSequence += 1;
    clearOptions("Повторите поиск после изменения запроса");
    status.textContent = "";
  });
  options.addEventListener("change", () => {
    bindButton.disabled = !options.value;
  });
  bindForm.addEventListener("submit", async (event) => {
    if (!options.value) {
      event.preventDefault();
      status.textContent = "Выберите проверенную карточку из списка.";
      return;
    }
    const editForm = document.getElementById("assignment-edit-form");
    const hasUnsavedChanges = editForm && Array.from(editForm.elements).some((control) => {
      if (!("value" in control) || control.type === "submit" || control.type === "button") return false;
      if (control.type === "checkbox" || control.type === "radio") return control.checked !== control.defaultChecked;
      if (control.tagName === "SELECT") return Array.from(control.options).some((option) => option.selected !== option.defaultSelected);
      return String(control.value) !== String(control.defaultValue ?? "");
    });
    if (hasUnsavedChanges) {
      event.preventDefault();
      status.textContent = "Сначала сохраните изменения полей назначения. Затем выберите карточку ещё раз.";
      return;
    }
    event.preventDefault();
    bindButton.disabled = true;
    status.textContent = "Сохраняем выбранную связь с проверенной карточкой…";
    try {
      const response = await fetch(bindForm.action, {
        method: "POST",
        credentials: "same-origin",
        redirect: "follow",
        headers: { Accept: "text/html", "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams(new FormData(bindForm)),
      });
      if (response.redirected && new URL(response.url).searchParams.get("saved") === "1") {
        window.location.assign(response.url);
        return;
      }
      let payload = null;
      try { payload = await response.json(); } catch { payload = null; }
      status.textContent = payload?.detail || `Не удалось связать карточку (HTTP ${response.status}). Черновик не изменён.`;
      bindButton.disabled = false;
    } catch (_error) {
      status.textContent = "Ответ о связывании не получен. Обновите страницу и проверьте состояние назначения перед повтором.";
      bindButton.disabled = false;
    }
  });
}

const sourceSelect = document.getElementById("assignment-source-select");
if (sourceSelect) {
  const preview = document.getElementById("assignment-source-preview");
  const download = document.getElementById("assignment-source-download");
  const current = document.getElementById("assignment-source-current");

  sourceSelect.addEventListener("change", () => {
    const option = sourceSelect.selectedOptions[0];
    if (!option || !preview || !download || !current) return;
    preview.src = option.dataset.previewUrl;
    download.href = option.dataset.downloadUrl;
    current.textContent = option.textContent;
  });
}

const packageEvidence = document.querySelector(".assignment-package-evidence");
if (packageEvidence) {
  const packageSelect = document.getElementById("assignment-package-select");
  const taskSelect = document.getElementById("assignment-package-task-select");
  const previous = document.getElementById("assignment-package-previous");
  const next = document.getElementById("assignment-package-next");
  const pageLabel = document.getElementById("assignment-package-page");
  const status = document.getElementById("assignment-package-evidence-status");
  const showButton = document.getElementById("assignment-package-show-document");
  const download = document.getElementById("assignment-package-download");
  const preview = document.getElementById("assignment-package-preview");
  const urlTemplate = packageEvidence.dataset.documentsUrlTemplate;
  let currentPage = 1;
  let pageCount = 1;
  let requestSequence = 0;

  function clearDocument() {
    preview.removeAttribute("src");
    preview.hidden = true;
    download.removeAttribute("href");
    download.hidden = true;
  }

  function resetTaskOptions(label) {
    taskSelect.replaceChildren(new Option(label, ""));
    taskSelect.disabled = true;
    showButton.disabled = true;
    previous.disabled = true;
    next.disabled = true;
    pageLabel.textContent = "Страница —";
    clearDocument();
  }

  async function loadPage(page) {
    const packageId = packageSelect.value;
    const sequence = ++requestSequence;
    resetTaskOptions(packageId ? "Загрузка заданий…" : "Сначала выберите папку");
    status.textContent = "";
    if (!packageId) return;
    const endpoint = urlTemplate.replace(/\/0\/documents$/, `/${encodeURIComponent(packageId)}/documents`);
    const url = new URL(endpoint, window.location.origin);
    url.searchParams.set("page", String(page));
    try {
      const response = await fetch(url, { headers: { Accept: "application/json" }, credentials: "same-origin" });
      const payload = await response.json();
      if (sequence !== requestSequence) return;
      if (!response.ok) {
        resetTaskOptions("Список недоступен");
        status.textContent = payload.detail || "Не удалось сверить состав папки. Обновите страницу.";
        return;
      }
      currentPage = payload.page;
      pageCount = payload.page_count;
      resetTaskOptions(payload.documents.length ? "Выберите точное задание" : "В папке нет активных заданий");
      for (const item of payload.documents) {
        const option = new Option(item.label, String(item.task_id));
        option.dataset.previewSupported = String(item.preview_supported);
        taskSelect.add(option);
      }
      taskSelect.disabled = payload.documents.length === 0;
      previous.disabled = currentPage <= 1;
      next.disabled = currentPage >= pageCount;
      pageLabel.textContent = `Страница ${currentPage} из ${pageCount}`;
      status.textContent = `${payload.package_title}: ${payload.total} заданий. Поля назначения остаются в форме без изменений.`;
    } catch (_error) {
      if (sequence !== requestSequence) return;
      resetTaskOptions("Список недоступен");
      status.textContent = "Не удалось загрузить список документов. Проверьте соединение и повторите.";
    }
  }

  packageSelect.addEventListener("change", () => loadPage(1));
  taskSelect.addEventListener("change", () => {
    requestSequence += 1;
    const option = taskSelect.selectedOptions[0];
    const previewSupported = option?.dataset.previewSupported === "true";
    showButton.disabled = !taskSelect.value;
    showButton.textContent = taskSelect.value && !previewSupported
      ? "Проверить и скачать оригинал"
      : "Показать выбранный документ";
    clearDocument();
    status.textContent = "";
  });
  previous.addEventListener("click", () => loadPage(Math.max(1, currentPage - 1)));
  next.addEventListener("click", () => loadPage(Math.min(pageCount, currentPage + 1)));
  showButton.addEventListener("click", async () => {
    const packageId = packageSelect.value;
    const taskId = taskSelect.value;
    if (!packageId || !taskId) return;
    const sequence = ++requestSequence;
    clearDocument();
    showButton.disabled = true;
    status.textContent = "Повторно сверяем членство перед открытием документа…";
    const endpoint = urlTemplate.replace(/\/0\/documents$/, `/${encodeURIComponent(packageId)}/documents`);
    const url = new URL(endpoint, window.location.origin);
    url.searchParams.set("page", String(currentPage));
    url.searchParams.set("task_id", taskId);
    try {
      const response = await fetch(url, { headers: { Accept: "application/json" }, credentials: "same-origin" });
      const payload = await response.json();
      if (sequence !== requestSequence) return;
      if (!response.ok) {
        const staleMessage = payload.detail || "Состав папки изменился. Обновите список.";
        clearDocument();
        await loadPage(currentPage);
        if (packageSelect.value === packageId) status.textContent = staleMessage;
        return;
      }
      const selected = payload.documents.find((item) => String(item.task_id) === taskId);
      if (!selected) {
        clearDocument();
        status.textContent = "Задание больше не состоит в этой папке. Обновите список.";
        await loadPage(currentPage);
        return;
      }
      if (!selected.preview_supported) {
        clearDocument();
        status.textContent = "Для этого формата доступно только скачивание оригинала.";
        download.href = selected.download_url;
        download.hidden = false;
        return;
      }
      preview.src = selected.preview_url;
      preview.hidden = false;
      download.href = selected.download_url;
      download.hidden = false;
      status.textContent = `Открыто: ${selected.label}. Документ не изменяет назначение.`;
    } catch (_error) {
      if (sequence === requestSequence) {
        clearDocument();
        status.textContent = "Не удалось проверить членство. Документ не открыт.";
      }
    } finally {
      if (sequence === requestSequence) {
        showButton.disabled = !taskSelect.value;
      }
    }
  });
}
