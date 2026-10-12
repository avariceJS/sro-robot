const extensionApi = globalThis.browser || globalThis.chrome;
document.querySelector('#search').addEventListener('submit', async event => {
  event.preventDefault();
  try {
    const url = SroKadProtocol.launchURL(document.querySelector('#case').value, crypto.randomUUID());
    await extensionApi.tabs.create({url});
    window.close();
  } catch {
    document.querySelector('#status').textContent = 'Проверьте номер дела: например, А99-12345/2099.';
  }
});
