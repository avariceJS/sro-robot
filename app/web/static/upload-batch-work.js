(() => {
  const form = document.getElementById("batch-reprocess-form");
  if (!form) return;

  const parentId = form.dataset.parentId;
  const keyField = document.getElementById("batch-reprocess-key");
  const status = document.getElementById("batch-reprocess-status");
  const button = form.querySelector("button[type=submit]");
  const storageKey = `sro-upload-reprocess:${parentId}`;

  const makeKey = () => {
    const bytes = new Uint8Array(24);
    crypto.getRandomValues(bytes);
    return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
  };

  try {
    let key = sessionStorage.getItem(storageKey);
    if (!key || !/^[0-9a-f]{48}$/.test(key)) {
      key = makeKey();
      sessionStorage.setItem(storageKey, key);
    }
    keyField.value = key;
    button.disabled = false;
  } catch {
    status.textContent = "Не удалось сохранить ключ повтора в этой вкладке. Обновите страницу и разрешите локальное хранилище.";
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!keyField.value) return;
    button.disabled = true;
    status.textContent = "Создаём отдельный план…";
    try {
      const response = await fetch(form.action, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({ request_key: keyField.value }),
      });
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(data.detail || `Ошибка ${response.status}`);
      }
      const data = await response.json();
      sessionStorage.removeItem(storageKey);
      window.location.assign(data.work_url);
    } catch (error) {
      status.textContent = `${error.message || "Не удалось создать план"}. Повторите действие: будет использован тот же ключ.`;
      button.disabled = false;
    }
  });
})();
