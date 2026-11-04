(function () {
  "use strict";

  const image = document.getElementById("source-image");
  const stage = image?.closest("[data-source-image-stage]");
  if (!image || !stage) return;
  const viewport = image.closest(".image-preview-viewport");

  function syncLayout() {
    const naturalWidth = Number(image.naturalWidth);
    const naturalHeight = Number(image.naturalHeight);
    const availableWidth = Number(viewport?.clientWidth);
    const availableHeight = Number(viewport?.clientHeight);
    if (!(naturalWidth > 0 && naturalHeight > 0 && availableWidth > 0 && availableHeight > 0)) return;
    const fit = Math.min(1, availableWidth / naturalWidth, availableHeight / naturalHeight);
    const width = naturalWidth * fit;
    const height = naturalHeight * fit;
    stage.style.width = `${width}px`;
    stage.style.height = `${height}px`;
    image.style.width = `${width}px`;
    image.style.height = `${height}px`;
    image.style.maxWidth = "none";
    image.style.maxHeight = "none";
  }

  image.addEventListener("load", syncLayout);
  image.addEventListener("error", () => {
    const trigger = activeTrigger;
    clearOverlay();
    if (trigger) report(trigger, "Предпросмотр документа недоступен.");
  });
  if (typeof ResizeObserver === "function" && viewport) {
    const resizeObserver = new ResizeObserver(syncLayout);
    resizeObserver.observe(viewport);
  }
  syncLayout();

  let overlay = null;
  let activeInput = null;
  let activeTrigger = null;
  const sourceInputIds = new Set();

  function clearOverlay() {
    overlay?.remove();
    overlay = null;
    activeInput = null;
    activeTrigger = null;
  }

  function boxesFor(trigger) {
    try {
      const boxes = JSON.parse(trigger.dataset.sourceBoxes || "[]");
      if (!Array.isArray(boxes)) return [];
      return boxes.filter((box) => {
        if (Number(box.page) !== 1) return false;
        return [box.x, box.y, box.width, box.height].every((value) => {
          const number = Number(value);
          return Number.isFinite(number) && number >= 0 && number <= 1;
        }) && Number(box.width) > 0 && Number(box.height) > 0
          && Number(box.x) + Number(box.width) <= 1.001
          && Number(box.y) + Number(box.height) <= 1.001;
      });
    } catch {
      return [];
    }
  }

  function report(trigger, message) {
    const notice = trigger.closest("td")?.querySelector("[data-field-source-notice]");
    if (!notice) return;
    notice.textContent = message;
    notice.hidden = !message;
  }

  function show(trigger, input) {
    clearOverlay();
    if (input.value !== trigger.dataset.sourceValue) {
      report(trigger, "Значение изменено вручную. Источник для него не размечен.");
      return;
    }
    if (image.isConnected === false || !(Number(image.naturalWidth) > 0 && Number(image.naturalHeight) > 0)) {
      report(trigger, "Предпросмотр документа недоступен.");
      return;
    }
    const boxes = boxesFor(trigger);
    if (!boxes.length || (image.dataset.loadedPage && image.dataset.loadedPage !== "1")) {
      report(trigger, "Фрагмент источника на этой странице недоступен.");
      return;
    }
    report(trigger, "");
    overlay = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    overlay.setAttribute("class", "source-image-overlay");
    overlay.setAttribute("viewBox", "0 0 1000 1000");
    overlay.setAttribute("preserveAspectRatio", "none");
    overlay.setAttribute("aria-hidden", "true");
    for (const box of boxes) {
      const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      rect.setAttribute("x", String(Number(box.x) * 1000));
      rect.setAttribute("y", String(Number(box.y) * 1000));
      rect.setAttribute("width", String(Number(box.width) * 1000));
      rect.setAttribute("height", String(Number(box.height) * 1000));
      overlay.append(rect);
    }
    stage.append(overlay);
    activeInput = input;
    activeTrigger = trigger;
    viewport?.scrollIntoView({ block: "nearest", inline: "nearest" });
    overlay.querySelector("rect")?.scrollIntoView({ block: "center", inline: "center" });
  }

  for (const trigger of document.querySelectorAll("[data-field-source-trigger]")) {
    const input = document.getElementById(trigger.dataset.fieldSourceTrigger);
    if (!input) continue;
    sourceInputIds.add(input.id);
    trigger.addEventListener("click", () => {
      input.focus();
      show(trigger, input);
    });
    input.addEventListener("focus", () => show(trigger, input));
    input.addEventListener("input", () => {
      if (activeInput === input || input.value !== trigger.dataset.sourceValue) {
        clearOverlay();
        report(trigger, "Значение изменено вручную. Источник для него не размечен.");
      }
    });
    input.addEventListener("change", () => {
      if (input.value !== trigger.dataset.sourceValue) {
        clearOverlay();
        report(trigger, "Значение изменено вручную. Источник для него не размечен.");
      }
    });
  }

  document.addEventListener("focusin", (event) => {
    const target = event.target;
    if (target && (target.tagName === "INPUT" || target.tagName === "SELECT")
        && !sourceInputIds.has(target.id)) clearOverlay();
  });
})();
