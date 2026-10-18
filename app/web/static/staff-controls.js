(function () {
  "use strict";

  let zoom = 1;
  let rotation = 0;

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
        input.focus();
      }
      return;
    }

    const image = document.getElementById("source-image");
    if (!image) return;

    const zoomButton = target.closest("[data-image-zoom]");
    if (zoomButton) {
      const action = zoomButton.dataset.imageZoom;
      zoom = action === "reset"
        ? 1
        : Math.min(3, Math.max(0.5, zoom + (action === "in" ? 0.25 : -0.25)));
      image.style.transform = `scale(${zoom}) rotate(${rotation}deg)`;
      return;
    }

    const rotateButton = target.closest("[data-image-rotate]");
    if (rotateButton) {
      rotation = (rotation + Number(rotateButton.dataset.imageRotate) + 360) % 360;
      image.style.transform = `scale(${zoom}) rotate(${rotation}deg)`;
    }
  });
})();
