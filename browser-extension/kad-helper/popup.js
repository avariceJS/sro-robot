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
const pdfPermissions = {permissions: ['webRequest', 'webRequestBlocking', 'webRequestFilterResponse']};
if (enablePdf) {
  if (!extensionApi.runtime?.getBrowserInfo) {
    enablePdf.disabled = true;
    enablePdf.textContent = 'Автоматическая передача PDF доступна в Firefox';
  } else {
    extensionApi.permissions.contains(pdfPermissions).then(granted => {
      if (granted) { enablePdf.disabled = true; enablePdf.textContent = 'Передача PDF включена'; }
    }).catch(() => { document.querySelector('#status').textContent = 'Не удалось проверить разрешения расширения.'; });
    enablePdf.addEventListener('click', async () => {
      try {
        const granted = await extensionApi.permissions.request(pdfPermissions);
        enablePdf.disabled = granted;
        document.querySelector('#status').textContent = granted
          ? 'Передача PDF включена. Вернитесь в сервис и запустите поиск.'
          : 'Без этого разрешения доступен просмотр PDF через расширение.';
      } catch { document.querySelector('#status').textContent = 'Не удалось включить передачу PDF. Проверьте разрешения расширения.'; }
    });
  }
}
