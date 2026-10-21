const extensionApi = globalThis.browser || globalThis.chrome;
const SERVICE_ORIGIN = /* SRO_SERVICE_ORIGIN */'http://localhost:8000';
const status = document.querySelector('#status');
document.querySelector('#open-office').addEventListener('click', async () => {
  try {
    await extensionApi.tabs.create({url: `${SERVICE_ORIGIN}/debtors/intake`});
    status.textContent = 'Откройте раздел «Документы и распознавание» и выполните поиск по номеру дела.';
  } catch {
    status.textContent = 'Не удалось открыть офисный сайт. Проверьте подключение и выполните вход.';
  }
});

const enablePdf = document.querySelector('#enable-pdf');
function pdfPermissions() {
  if (typeof extensionApi.runtime?.getBrowserInfo === 'function') {
    return {permissions: ['webRequest', 'webRequestBlocking', 'webRequestFilterResponse']};
  }
  const agent = globalThis.navigator?.userAgent || '';
  if (/Chrome\/|Edg\//.test(agent) && !/Firefox\//.test(agent)) return {permissions: ['debugger']};
  return null;
}
if (enablePdf) {
  const requested = pdfPermissions();
  if (!requested) {
    status.textContent = 'Этот браузер не поддерживает автоматическую передачу PDF. Откройте СРО Робот в Chrome, Edge или Firefox.';
  } else {
    if (requested.permissions[0] === 'debugger') {
      // Chrome 121+ rejects debugger as an optional permission, so the popup cannot grant it.
      extensionApi.permissions.contains(requested).then(granted => {
        enablePdf.style.display = 'none';
        status.textContent = granted
          ? 'Передача PDF готова. Chrome покажет системную полосу во время чтения выбранного документа.'
          : 'Chrome не подтвердил разрешение для передачи PDF. Проверьте страницу расширений и его доступ.';
      }).catch(() => { status.textContent = 'Не удалось проверить разрешение браузера.'; });
    } else {
      extensionApi.permissions.contains(requested).then(granted => {
        enablePdf.style.display = granted ? 'none' : 'block';
        if (granted) status.textContent = 'Передача PDF готова. Выполните поиск на странице СРО Робот.';
      }).catch(() => { status.textContent = 'Не удалось проверить разрешение браузера.'; });
      enablePdf.addEventListener('click', async () => {
        try {
          const granted = await extensionApi.permissions.request(requested);
          enablePdf.style.display = granted ? 'none' : 'block';
          status.textContent = granted
            ? 'Передача PDF включена. Вернитесь в СРО Робот; страница обновится.'
            : 'Без разрешения автоматическая передача PDF остановлена.';
        } catch { status.textContent = 'Не удалось включить передачу PDF. Проверьте разрешения расширения.'; }
      });
    }
  }
}
