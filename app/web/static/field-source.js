(function () {
  "use strict";
  const image = document.getElementById("source-image");
  const stage = image?.closest("[data-source-image-stage]");
  if (!image || !stage) return;
  const viewport = image.closest(".image-preview-viewport");
  const workspace = stage.closest(".review-workspace");
  const fields = [];
  let imageOverlay = null;
  let pointers = null;
  let active = null;
  let unavailable = false;
  const palette = ["#2457a7", "#a13e83", "#16805d", "#b45c12", "#6741a5", "#087e8b"];

  function boxesFor(marker) {
    try {
      const boxes = JSON.parse(marker.dataset.sourceBoxes || "[]");
      return Array.isArray(boxes) ? boxes.filter((box) => Number(box.page) === 1
        && [box.x, box.y, box.width, box.height].every((v) => Number.isFinite(Number(v)) && Number(v) >= 0 && Number(v) <= 1)
        && Number(box.width) > 0 && Number(box.height) > 0
        && Number(box.x) + Number(box.width) <= 1.001 && Number(box.y) + Number(box.height) <= 1.001) : [];
    } catch { return []; }
  }
  function report(item, message) {
    const notice = item.marker.closest("td")?.querySelector("[data-field-source-notice]");
    if (notice) { notice.textContent = message; notice.hidden = !message; }
  }
  function syncLayout() {
    const nw = Number(image.naturalWidth), nh = Number(image.naturalHeight);
    const aw = Number(viewport?.clientWidth), ah = Number(viewport?.clientHeight);
    if (!(nw > 0 && nh > 0 && aw > 0 && ah > 0)) return;
    const fit = Math.min(1, aw / nw, ah / nh), width = nw * fit, height = nh * fit;
    stage.style.width = `${width}px`; stage.style.height = `${height}px`;
    image.style.width = `${width}px`; image.style.height = `${height}px`;
    image.style.maxWidth = "none"; image.style.maxHeight = "none";
    render();
  }
  function render() {
    if (unavailable || image.isConnected === false || !(image.naturalWidth > 0 && image.naturalHeight > 0)) return;
    imageOverlay?.remove(); imageOverlay = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    imageOverlay.setAttribute("class", "source-image-overlay"); imageOverlay.setAttribute("viewBox", "0 0 1000 1000"); imageOverlay.setAttribute("preserveAspectRatio", "none"); imageOverlay.setAttribute("aria-hidden", "true");
    const workspaceRect = workspace?.getBoundingClientRect();
    if (workspace && workspaceRect) {
      pointers?.remove(); pointers = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      pointers.setAttribute("class", "source-pointer-lines"); pointers.setAttribute("aria-hidden", "true");
      pointers.style.width = `${workspaceRect.width}px`; pointers.style.height = `${workspaceRect.height}px`; workspace.append(pointers);
      const defs = document.createElementNS("http://www.w3.org/2000/svg", "defs");
      const arrow = document.createElementNS("http://www.w3.org/2000/svg", "marker");
      arrow.setAttribute("id", "source-pointer-arrow"); arrow.setAttribute("markerWidth", "7"); arrow.setAttribute("markerHeight", "7"); arrow.setAttribute("refX", "6"); arrow.setAttribute("refY", "3.5"); arrow.setAttribute("orient", "auto");
      const head = document.createElementNS("http://www.w3.org/2000/svg", "path"); head.setAttribute("d", "M0,0 L7,3.5 L0,7 z"); arrow.append(head); defs.append(arrow); pointers.append(defs);
    }
    fields.forEach((item, index) => {
      item.stale = item.input.value !== item.marker.dataset.sourceValue;
      item.marker.hidden = item.stale;
      if (item.stale) return;
      const boxes = boxesFor(item.marker);
      if (!boxes.length || (image.dataset.loadedPage && image.dataset.loadedPage !== "1")) { item.marker.hidden = true; return; }
      item.marker.hidden = false;
      report(item, "");
      boxes.forEach((box) => {
        const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
        rect.setAttribute("x", String(Number(box.x) * 1000)); rect.setAttribute("y", String(Number(box.y) * 1000));
        rect.setAttribute("width", String(Number(box.width) * 1000)); rect.setAttribute("height", String(Number(box.height) * 1000)); rect.dataset.sourceIndex = String(index);
        rect.style.stroke = palette[index % palette.length]; imageOverlay.append(rect);
      });
    });
    stage.append(imageOverlay);
    if (pointers && workspaceRect) fields.forEach((item, index) => {
      if (item.stale || item.marker.hidden) return;
      const sourceMarker = imageOverlay.querySelectorAll(`rect[data-source-index="${index}"]`)[0];
      const sourceRect = sourceMarker?.getBoundingClientRect(), target = item.marker.getBoundingClientRect();
      if (!sourceRect || !target.width || !target.height) return;
      const clip = viewport?.getBoundingClientRect?.();
      if (clip && (sourceRect.right <= clip.left || sourceRect.left >= clip.right
        || sourceRect.bottom <= clip.top || sourceRect.top >= clip.bottom)) return;
      const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
      const sourceX = clip ? Math.min(sourceRect.right, clip.right) : sourceRect.right;
      const centerY = sourceRect.top + sourceRect.height / 2;
      const sourceY = clip ? Math.max(clip.top, Math.min(centerY, clip.bottom)) : centerY;
      line.setAttribute("x1", String(sourceX - workspaceRect.left)); line.setAttribute("y1", String(sourceY - workspaceRect.top));
      line.setAttribute("x2", String(target.left - workspaceRect.left)); line.setAttribute("y2", String(target.top + target.height / 2 - workspaceRect.top)); line.setAttribute("stroke", palette[index % palette.length]); line.dataset.sourceIndex = String(index); pointers.append(line);
    });
    setActive(active);
  }
  function setActive(item) {
    if (item && (item.stale || item.marker.hidden || unavailable)) item = null;
    active = item;
    for (const el of [imageOverlay, pointers]) if (el) {
      el.classList.toggle("has-active", Boolean(item));
      for (const node of el.querySelectorAll("[data-source-index]")) node.classList.toggle("is-active", Number(node.dataset.sourceIndex) === fields.indexOf(item));
    }
  }
  for (const marker of document.querySelectorAll("[data-field-source-marker]")) {
    const input = document.getElementById(marker.dataset.fieldSourceMarker);
    if (!input) continue;
    const item = { marker, input, stale: false }; fields.push(item);
    marker.dataset.sourceIndex = String(fields.length - 1);
    marker.style.backgroundColor = palette[(fields.length - 1) % palette.length];
    input.addEventListener("focus", () => setActive(item)); input.addEventListener("blur", () => setActive(null));
    marker.addEventListener("mouseenter", () => setActive(item)); marker.addEventListener("mouseleave", () => setActive(null));
    const label = marker.closest("td")?.querySelector("label");
    label?.addEventListener("mouseenter", () => setActive(item)); label?.addEventListener("mouseleave", () => setActive(null));
    input.addEventListener("input", () => {
      item.stale = input.value !== marker.dataset.sourceValue;
      report(item, item.stale ? "Значение изменено вручную. Источник для него не размечен." : ""); render();
    });
    input.addEventListener("change", () => {
      item.stale = input.value !== marker.dataset.sourceValue;
      report(item, item.stale ? "Значение изменено вручную. Источник для него не размечен." : ""); render();
    });
  }
  image.addEventListener("load", () => { unavailable = false; for (const item of fields) { if (item.previewError) { item.previewError = false; report(item, ""); } } syncLayout(); });
  image.addEventListener("error", () => { unavailable = true; imageOverlay?.remove(); imageOverlay = null; pointers?.remove(); pointers = null; for (const item of fields) { item.marker.hidden = true; item.previewError = true; report(item, "Предпросмотр документа недоступен."); } });
  if (typeof ResizeObserver === "function" && viewport) new ResizeObserver(syncLayout).observe(viewport);
  workspace?.addEventListener("scroll", render, true);
  stage.addEventListener("transitionend", render);
  if (typeof window !== "undefined") {
    window.addEventListener("resize", render);
    window.addEventListener("scroll", render, { passive: true });
  }
  if (typeof MutationObserver === "function") new MutationObserver(render).observe(stage, { attributes: true, attributeFilter: ["style"] });
  syncLayout();
})();
