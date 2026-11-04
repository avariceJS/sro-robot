(function () {
  "use strict";

  let zoom = 1;
  let rotation = 0;

  for (const viewer of document.querySelectorAll("[data-tiff-preview]")) {
    const image = viewer.querySelector("#source-image");
    const label = viewer.querySelector("[data-tiff-page-label]");
    const notice = viewer.querySelector("[data-tiff-page-notice]");
    const previousButton = viewer.querySelector('[data-tiff-page-step="-1"]');
    const nextButton = viewer.querySelector('[data-tiff-page-step="1"]');
    const previewUrl = viewer.dataset.previewUrl;
    if (!image || !label || !notice || !previousButton || !nextButton || !previewUrl) continue;

    let currentPage = 1;
    let pageCount = 0;
    let loading = false;
    let requestId = 0;
    let objectUrl = null;

    async function loadPage(page) {
      if (loading || page < 1 || (pageCount > 0 && page > pageCount)) return;
      loading = true;
      const thisRequest = ++requestId;
      previousButton.disabled = true;
      nextButton.disabled = true;
      notice.hidden = true;
      label.textContent = `Загрузка страницы ${page}…`;
      try {
        let response;
        for (let attempt = 0; attempt < 2; attempt += 1) {
          response = await fetch(`${previewUrl}?page=${page}`, {
            credentials: "same-origin",
            cache: "no-store",
            headers: { Accept: "image/png" },
          });
          if (response.status !== 503 || attempt === 1) break;
          const retryAfter = Number(response.headers.get("Retry-After"));
          const delayMs = Number.isFinite(retryAfter)
            ? Math.max(0, Math.min(2000, retryAfter * 1000))
            : 1000;
          await new Promise((resolve) => window.setTimeout(resolve, delayMs));
        }
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const count = Number(response.headers.get("X-Document-Page-Count"));
        const blob = await response.blob();
        if (thisRequest !== requestId) return;
        if (!Number.isInteger(count) || count < 1 || blob.type !== "image/png") {
          throw new Error("Invalid preview response");
        }
        const nextObjectUrl = URL.createObjectURL(blob);
        image.addEventListener("load", () => {
          image.dataset.loadedPage = String(page);
          if (objectUrl) URL.revokeObjectURL(objectUrl);
          objectUrl = nextObjectUrl;
        }, { once: true });
        image.src = nextObjectUrl;
        currentPage = page;
        pageCount = count;
        label.textContent = `Страница ${currentPage} из ${pageCount}`;
        image.alt = `Страница документа ${currentPage} из ${pageCount}`;
      } catch {
        if (thisRequest !== requestId) return;
        label.textContent = pageCount > 0
          ? `Страница ${currentPage} из ${pageCount}`
          : "Предпросмотр недоступен";
        notice.textContent = "Страница не загрузилась. Скачайте оригинал выше и повторите попытку позже.";
        notice.hidden = false;
      } finally {
        if (thisRequest === requestId) {
          loading = false;
          previousButton.disabled = currentPage <= 1;
          nextButton.disabled = pageCount === 0 || currentPage >= pageCount;
        }
      }
    }

    previousButton.addEventListener("click", () => loadPage(currentPage - 1));
    nextButton.addEventListener("click", () => loadPage(currentPage + 1));
    loadPage(1);
  }

  function hasUnsavedChanges(form) {
    return Array.from(form.elements || []).some((control) => {
      if (!("value" in control)) return false;
      if (control.type === "checkbox" || control.type === "radio") {
        return control.checked !== control.defaultChecked;
      }
      if (control.tagName === "SELECT") {
        return Array.from(control.options || []).some(
          (option) => option.selected !== option.defaultSelected,
        );
      }
      return String(control.value) !== String(control.defaultValue ?? "");
    });
  }

  const guardedForms = new Map();
  for (const form of document.querySelectorAll("form[data-unsaved-guard]")) {
    const state = { submitting: false };
    form.addEventListener("submit", () => {
      state.submitting = true;
    });
    if (form.id) guardedForms.set(form.id, state);
    window.addEventListener("beforeunload", (event) => {
      const initiallyDirty = form.dataset?.unsavedInitiallyDirty === "true";
      if (state.submitting || (!initiallyDirty && !hasUnsavedChanges(form))) return;
      event.preventDefault();
      event.returnValue = "";
    });
  }

  for (const form of document.querySelectorAll("form[data-unsaved-submit-target]")) {
    const state = guardedForms.get(form.dataset.unsavedSubmitTarget);
    if (state) {
      form.addEventListener("submit", () => {
        state.submitting = true;
      });
    }
  }

  document.addEventListener("error", (event) => {
    const image = event.target;
    if (!(image instanceof HTMLImageElement) || image.id !== "source-image") return;
    const viewport = image.closest(".image-preview-viewport");
    if (!viewport) return;
    const notice = document.createElement("p");
    notice.className = "field-warning";
    notice.setAttribute("role", "status");
    notice.textContent = "Предпросмотр недоступен или превышает безопасный размер. Скачайте оригинал выше.";
    image.replaceWith(notice);
  }, true);

  document.addEventListener("click", (event) => {
    const target = event.target;
    if (!(target instanceof Element)) return;

    const conflictButton = target.closest("[data-conflict-copy]");
    if (conflictButton) {
      const form = document.getElementById(conflictButton.dataset.form);
      const input = form?.elements.namedItem(conflictButton.dataset.target);
      if (input && "value" in input) {
        input.value = conflictButton.dataset.value;
        input.dispatchEvent(new Event("input", { bubbles: true }));
        input.focus();
      }
      return;
    }

    const image = document.getElementById("source-image");
    if (!image) return;
    const transformTarget = image.closest("[data-source-image-stage]") || image;

    const zoomButton = target.closest("[data-image-zoom]");
    if (zoomButton) {
      const action = zoomButton.dataset.imageZoom;
      zoom = action === "reset"
        ? 1
        : Math.min(3, Math.max(0.5, zoom + (action === "in" ? 0.25 : -0.25)));
      transformTarget.style.transform = `scale(${zoom}) rotate(${rotation}deg)`;
      const zoomLabel = document.querySelector('[data-image-zoom="reset"]');
      if (zoomLabel) zoomLabel.textContent = `${Math.round(zoom * 100)}%`;
      return;
    }

    const rotateButton = target.closest("[data-image-rotate]");
    if (rotateButton) {
      rotation = (rotation + Number(rotateButton.dataset.imageRotate) + 360) % 360;
      transformTarget.style.transform = `scale(${zoom}) rotate(${rotation}deg)`;
    }
  });
})();
