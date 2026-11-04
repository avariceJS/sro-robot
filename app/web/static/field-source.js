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
  let hovered = null;
  let pinned = null;
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
  function displayBoxesFor(marker) {
    const boxes = boxesFor(marker);
    if (marker.dataset.fieldSourceMarker === "address") {
      // Join consecutive words on a printed line without enclosing unrelated
      // text alongside the short apartment continuation on the next line.
      const lines = [];
      for (const box of boxes) {
        const previous = lines.at(-1), last = previous?.at(-1);
        // Neighboring words can follow a tilted baseline on a photographed
        // page; a shared intersection across the entire line is too strict.
        if (last && Number(box.x) >= Number(last.x)
          && Math.min(Number(last.y) + Number(last.height), Number(box.y) + Number(box.height))
            > Math.max(Number(last.y), Number(box.y))) previous.push(box);
        else lines.push([box]);
      }
      return lines.map((words) => ({ words }));
    }
    // A date on one printed line is one source fragment, including its spaces.
    if (marker.dataset.fieldSourceMarker === "doc_date" && boxes.length > 1) {
      const top = Math.max(...boxes.map((box) => Number(box.y)));
      const bottom = Math.min(...boxes.map((box) => Number(box.y) + Number(box.height)));
      if (bottom > top) {
        const x = Math.min(...boxes.map((box) => Number(box.x)));
        const y = Math.min(...boxes.map((box) => Number(box.y)));
        return [{ page: 1, x, y,
          width: Math.max(...boxes.map((box) => Number(box.x) + Number(box.width))) - x,
          height: Math.max(...boxes.map((box) => Number(box.y) + Number(box.height))) - y }];
      }
    }
    return boxes;
  }
  function outlinePadding() {
    // Keep a full CSS pixel outside the glyphs, also after zoom or rotation.
    const matrix = imageOverlay.getScreenCTM?.();
    const scaleX = matrix ? Math.hypot(matrix.a, matrix.b) : image.clientWidth / 1000;
    const scaleY = matrix ? Math.hypot(matrix.c, matrix.d) : image.clientHeight / 1000;
    return { x: scaleX > 0 ? 1 / scaleX : 0, y: scaleY > 0 ? 1 / scaleY : 0 };
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
      pointers.setAttribute("class", "source-pointer-lines");
      pointers.style.width = `${workspaceRect.width}px`; pointers.style.height = `${workspaceRect.height}px`; workspace.append(pointers);
      const defs = document.createElementNS("http://www.w3.org/2000/svg", "defs");
      const arrow = document.createElementNS("http://www.w3.org/2000/svg", "marker");
      arrow.setAttribute("id", "source-pointer-arrow"); arrow.setAttribute("markerWidth", "7"); arrow.setAttribute("markerHeight", "7"); arrow.setAttribute("refX", "6"); arrow.setAttribute("refY", "3.5"); arrow.setAttribute("orient", "auto");
      const head = document.createElementNS("http://www.w3.org/2000/svg", "path"); head.setAttribute("d", "M0,0 L7,3.5 L0,7 z"); arrow.append(head); defs.append(arrow); pointers.append(defs);
    }
    stage.append(imageOverlay);
    const padding = outlinePadding();
    fields.forEach((item, index) => {
      item.stale = item.input.value !== item.marker.dataset.sourceValue;
      item.available = false;
      if (item.stale) return;
      const boxes = displayBoxesFor(item.marker);
      if (!boxes.length || (image.dataset.loadedPage && image.dataset.loadedPage !== "1")) return;
      item.available = true;
      report(item, "");
      boxes.forEach((box) => {
        if (box.words) {
          const points = box.words.flatMap((word, wordIndex) => {
            const y = (Number(word.y) + Number(word.height)) * 1000 + padding.y;
            const left = Number(word.x) * 1000 - (wordIndex === 0 ? padding.x : 0);
            const right = (Number(word.x) + Number(word.width)) * 1000
              + (wordIndex === box.words.length - 1 ? padding.x : 0);
            return [[left, y], [right, y]];
          });
          const underline = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
          underline.setAttribute("points", points.map((point) => point.join(",")).join(" "));
          underline.dataset.sourceIndex = String(index);
          underline.dataset.sourceEndX = String(points.at(-1)[0]);
          underline.dataset.sourceEndY = String(points.at(-1)[1]);
          underline.style.stroke = palette[index % palette.length];
          imageOverlay.append(underline);
          return;
        }
        const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
        rect.setAttribute("x", String(Number(box.x) * 1000 - padding.x)); rect.setAttribute("y", String(Number(box.y) * 1000 - padding.y));
        rect.setAttribute("width", String(Number(box.width) * 1000 + 2 * padding.x)); rect.setAttribute("height", String(Number(box.height) * 1000 + 2 * padding.y)); rect.dataset.sourceIndex = String(index);
        rect.style.stroke = palette[index % palette.length]; imageOverlay.append(rect);
      });
    });
    if (pointers && workspaceRect) fields.forEach((item, index) => {
      if (item.stale || !item.available) return;
      const sourceMarker = Array.from(imageOverlay.querySelectorAll(`[data-source-index="${index}"]`)).at(-1);
      let sourceRect = sourceMarker?.getBoundingClientRect();
      if (sourceMarker?.dataset.sourceEndX !== undefined) {
        const matrix = sourceMarker.getScreenCTM?.();
        if (matrix) {
          const x = Number(sourceMarker.dataset.sourceEndX), y = Number(sourceMarker.dataset.sourceEndY);
          const screenX = matrix.a * x + matrix.c * y + matrix.e;
          const screenY = matrix.b * x + matrix.d * y + matrix.f;
          sourceRect = { left: screenX, right: screenX, top: screenY, bottom: screenY, height: 0 };
        }
      }
      const target = item.label?.getBoundingClientRect();
      if (!sourceRect || !target?.width || !target.height) return;
      const clip = viewport?.getBoundingClientRect?.();
      if (clip && (sourceRect.right <= clip.left || sourceRect.left >= clip.right
        || sourceRect.bottom <= clip.top || sourceRect.top >= clip.bottom)) return;
      const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
      const sourceX = clip ? Math.min(sourceRect.right, clip.right) : sourceRect.right;
      const centerY = sourceRect.top + sourceRect.height / 2;
      const sourceY = clip ? Math.max(clip.top, Math.min(centerY, clip.bottom)) : centerY;
      const x1 = String(sourceX - workspaceRect.left), y1 = String(sourceY - workspaceRect.top);
      const x2 = String(target.left - 4 - workspaceRect.left), y2 = String(target.top + target.height / 2 - workspaceRect.top);
      line.setAttribute("x1", x1); line.setAttribute("y1", y1); line.setAttribute("x2", x2); line.setAttribute("y2", y2);
      line.setAttribute("stroke", palette[index % palette.length]); line.dataset.sourceIndex = String(index); line.setAttribute("aria-hidden", "true"); pointers.append(line);
      const hit = document.createElementNS("http://www.w3.org/2000/svg", "line");
      hit.setAttribute("x1", x1); hit.setAttribute("y1", y1); hit.setAttribute("x2", x2); hit.setAttribute("y2", y2);
      hit.setAttribute("class", "source-pointer-hit"); hit.setAttribute("role", "button"); hit.setAttribute("tabindex", "0");
      hit.setAttribute("aria-label", `Закрепить источник: ${item.marker.dataset.fieldSourceTitle || item.input.name || item.input.id}`);
      hit.setAttribute("aria-pressed", String(pinned === item)); hit.dataset.sourceIndex = String(index);
      hit.addEventListener("click", () => togglePin(item));
      hit.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); togglePin(item); } });
      hit.addEventListener("mouseenter", () => setHover(item)); hit.addEventListener("mouseleave", () => { hovered = null; setActive(); });
      hit.addEventListener("focus", () => setHover(item)); hit.addEventListener("blur", () => { hovered = null; setActive(); });
      pointers.append(hit);
    });
    setActive();
  }
  function setActive() {
    let item = pinned || hovered;
    if (item && (item.stale || !item.available || unavailable)) item = null;
    for (const el of [imageOverlay, pointers]) if (el) {
      el.classList.toggle("has-active", Boolean(item));
      for (const node of el.querySelectorAll("[data-source-index]")) node.classList.toggle("is-active", Number(node.dataset.sourceIndex) === fields.indexOf(item));
    }
  }
  function setHover(item) { hovered = item; setActive(); }
  function togglePin(item) {
    if (item.stale || !item.available || unavailable) return;
    pinned = pinned === item ? null : item;
    if (!pinned) hovered = null;
    setActive();
    for (const hit of pointers?.querySelectorAll(".source-pointer-hit") || []) hit.setAttribute("aria-pressed", String(Number(hit.dataset.sourceIndex) === fields.indexOf(pinned)));
  }
  for (const marker of document.querySelectorAll("[data-field-source-marker]")) {
    const input = document.getElementById(marker.dataset.fieldSourceMarker);
    if (!input) continue;
    const label = marker.closest("td")?.querySelector("label");
    const item = { marker, input, label, stale: false }; fields.push(item);
    marker.dataset.sourceIndex = String(fields.length - 1);
    input.addEventListener("focus", () => setHover(item)); input.addEventListener("blur", () => { hovered = null; setActive(); });
    input.addEventListener("mouseenter", () => setHover(item)); input.addEventListener("mouseleave", () => { hovered = null; setActive(); });
    label?.addEventListener("mouseenter", () => setHover(item)); label?.addEventListener("mouseleave", () => { hovered = null; setActive(); });
    label?.addEventListener("click", (event) => {
      event.preventDefault(); // Toggle the source instead of moving input focus.
      togglePin(item);
    });
    if (label) label.style.cursor = "pointer";
    input.addEventListener("input", () => {
      item.stale = input.value !== marker.dataset.sourceValue;
      if (item.stale && pinned === item) pinned = null;
      report(item, item.stale ? "Значение изменено вручную. Источник для него не размечен." : ""); render();
    });
    input.addEventListener("change", () => {
      item.stale = input.value !== marker.dataset.sourceValue;
      if (item.stale && pinned === item) pinned = null;
      report(item, item.stale ? "Значение изменено вручную. Источник для него не размечен." : ""); render();
    });
  }
  image.addEventListener("load", () => { unavailable = false; for (const item of fields) { if (item.previewError) { item.previewError = false; report(item, item.stale ? "Значение изменено вручную. Источник для него не размечен." : ""); } } syncLayout(); });
  image.addEventListener("error", () => { unavailable = true; pinned = null; hovered = null; imageOverlay?.remove(); imageOverlay = null; pointers?.remove(); pointers = null; for (const item of fields) { item.previewError = !item.stale; if (!item.stale) report(item, "Предпросмотр документа недоступен."); } });
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
