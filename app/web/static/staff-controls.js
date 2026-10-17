(function () {
  "use strict";

  let zoom = 1;
  let rotation = 0;

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
