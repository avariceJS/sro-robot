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
  window.setTimeout(() => {
    const status = document.querySelector('#kad-helper-connection-status');
    if (status?.dataset.state === 'checking') {
      status.textContent = status.dataset.storesReady === 'true'
        ? 'Помощник не обнаружен. Установите его из официального магазина; после возврата страница проверит подключение.'
        : 'Помощник не обнаружен. Установка появится после публикации в официальных магазинах.';
      status.dataset.state = 'unavailable';
    }
  }, 1500);
  const status = document.querySelector('#kad-helper-connection-status');
  const indicator = document.querySelector('#kad-helper-connection-indicator');
  if (status && indicator) {
    const updateIndicator = () => {
      indicator.classList.remove('bg-slate-400', 'bg-green-500', 'bg-amber-500');
      indicator.classList.add(status.dataset.state === 'ready' ? 'bg-green-500' : status.dataset.state === 'checking' ? 'bg-slate-400' : 'bg-amber-500');
    };
    updateIndicator();
    new MutationObserver(updateIndicator).observe(status, {attributes: true, attributeFilter: ['data-state']});
  }
})();
