"use strict";

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
