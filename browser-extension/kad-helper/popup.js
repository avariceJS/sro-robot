const extensionApi = globalThis.browser || globalThis.chrome;
document.querySelector('#search').addEventListener('submit', async event => {
  event.preventDefault();
  try {
    const url = SroKadProtocol.launchURL(
      document.querySelector('#case').value,
      crypto.randomUUID(),
      document.querySelector('#act-date').value || null,
    );
    await extensionApi.tabs.create({url});
    window.close();
  } catch {
    document.querySelector('#status').textContent = 'Проверьте номер дела и дату судебного акта.';
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
    enablePdf.disabled = true;
    enablePdf.textContent = 'Автоматическая передача PDF недоступна в этом браузере';
  } else {
    if (requested.permissions[0] === 'debugger') {
      // Chrome 121+ rejects debugger as an optional permission, so the popup cannot grant it.
      enablePdf.disabled = true;
      extensionApi.permissions.contains(requested).then(granted => {
        enablePdf.textContent = granted ? 'Передача PDF включена' : 'Обновите расширение в Chrome';
        if (!granted) document.querySelector('#status').textContent = 'Откройте chrome://extensions, обновите помощник и подтвердите разрешение. Затем откройте это окно снова.';
      }).catch(() => { document.querySelector('#status').textContent = 'Не удалось проверить разрешения расширения.'; });
    } else {
      extensionApi.permissions.contains(requested).then(granted => {
        if (granted) { enablePdf.disabled = true; enablePdf.textContent = 'Передача PDF включена'; }
      }).catch(() => { document.querySelector('#status').textContent = 'Не удалось проверить разрешения расширения.'; });
      enablePdf.addEventListener('click', async () => {
        try {
          const granted = await extensionApi.permissions.request(requested);
          enablePdf.disabled = granted;
          enablePdf.textContent = granted ? 'Передача PDF включена' : enablePdf.textContent;
          document.querySelector('#status').textContent = granted
            ? 'Передача PDF включена. Вернитесь в сервис и запустите поиск.'
            : 'Без этого разрешения доступен просмотр PDF через расширение.';
        } catch { document.querySelector('#status').textContent = 'Не удалось включить передачу PDF. Проверьте разрешения расширения.'; }
      });
    }
  }
}
