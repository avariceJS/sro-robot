/* The installed local-service content script owns automatic acquisition. */
(() => {
  const form = document.querySelector('#kad-browser-search');
  if (!form) return;
  // Ancestor capture runs before old helper versions' form-level submit handler.
  // Never let a helper without date support silently turn this into case-only intake.
  document.addEventListener('submit', event => {
    if (event.target !== form || !form.elements.act_date?.value || form.dataset.kadActDate === 'ready') return;
    event.preventDefault();
    event.stopImmediatePropagation();
    document.querySelector('#kad-browser-status').textContent = 'Для поиска по дате обновите помощник до версии 0.2.2 или новее и обновите страницу.';
  }, true);
  form.addEventListener('submit', event => {
    event.preventDefault();
    if (form.dataset.kadBridge === 'ready') return;
    document.querySelector('#kad-browser-status').textContent = 'Подключите обновлённый помощник и откройте сервис в Firefox по http://localhost:8000. Затем обновите эту страницу.';
  });
})();
