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
