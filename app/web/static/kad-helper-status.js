(() => {
  const installIntentKey = 'sro-kad-helper-install-return-v1';
  let wasHidden = document.visibilityState === 'hidden';
  document.addEventListener('click', event => {
    if (event.target instanceof Element && event.target.closest('[data-kad-store-link]')) {
      try { sessionStorage.setItem(installIntentKey, '1'); } catch { /* optional readiness hint */ }
    }
  });
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') {
      wasHidden = true;
      return;
    }
    if (!wasHidden) return;
    wasHidden = false;
    try {
      if (sessionStorage.getItem(installIntentKey) === '1') {
        sessionStorage.removeItem(installIntentKey);
        location.reload();
      }
    } catch { /* optional readiness hint */ }
  });

  const status = document.querySelector('#kad-helper-connection-status');
  const indicator = document.querySelector('#kad-helper-connection-indicator');
  const install = document.querySelector('#kad-helper-install');
  const unconfigured = document.querySelector('#kad-helper-install-unconfigured');
  const connected = document.querySelector('#kad-helper-connected');
  if (!status) return;

  const applyState = () => {
    const state = status.dataset.state;
    if (indicator) indicator.dataset.state = state;
    if (install) install.hidden = state !== 'unavailable';
    if (unconfigured) unconfigured.hidden = state !== 'unavailable';
    if (connected) connected.hidden = state !== 'ready';
  };
  applyState();
  new MutationObserver(applyState).observe(status, {attributes: true, attributeFilter: ['data-state']});

  window.setTimeout(() => {
    if (status.dataset.state !== 'checking') return;
    status.textContent = status.dataset.storesReady === 'true'
      ? 'Помощник не обнаружен. Установите его из магазина и вернитесь сюда.'
      : 'Помощник не обнаружен. Ссылка установки пока не настроена.';
    status.dataset.state = 'unavailable';
    applyState();
  }, 1500);
})();
