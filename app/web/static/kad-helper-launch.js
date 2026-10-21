/* The installed service content script owns automatic acquisition. */
(() => {
  const form = document.querySelector('#kad-browser-search');
  if (!form) return;
  const button = form.querySelector('button[type="submit"]');
  if (button && !(form.dataset.kadProtocol === '1' && form.dataset.kadBridge === 'ready')) button.disabled = true;
  // Run before legacy form listeners; no search starts without the current handshake.
  document.addEventListener('submit', event => {
    if (event.target !== form || form.dataset.kadProtocol === '1' && form.dataset.kadBridge === 'ready') return;
    event.preventDefault();
    event.stopImmediatePropagation();
    document.querySelector('#kad-browser-status').textContent = 'Помощник не подключён или его версия несовместима. Установите версию помощника из официального магазина и обновите страницу.';
  }, true);
  form.addEventListener('submit', event => {
    event.preventDefault();
    document.querySelector('#kad-browser-status').textContent = 'Помощник не подключён. Откройте страницу настройки и установите его из официального магазина.';
  });
  window.setTimeout(() => {
    const status = document.querySelector('#kad-browser-status');
    if (form.dataset.kadBridge !== 'ready' && (!status.dataset.state || status.dataset.state === 'checking')) {
      status.textContent = 'Помощник не подключён. Настройте его для этого браузера, затем вернитесь к поиску.';
      status.dataset.state = 'unavailable';
    }
  }, 1500);
})();
