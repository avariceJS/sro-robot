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
