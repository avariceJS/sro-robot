/* The installed local-service content script owns automatic acquisition. */
(() => {
  const form = document.querySelector('#kad-browser-search');
  if (!form) return;
  form.addEventListener('submit', event => {
    event.preventDefault();
    if (form.dataset.kadBridge === 'ready') return;
    document.querySelector('#kad-browser-status').textContent = 'Подключите обновлённый помощник и откройте сервис в Firefox по http://localhost:8000. Затем обновите эту страницу.';
  });
})();
