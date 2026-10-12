/* The fragment is consumed locally by the installed KAD-only extension. */
(() => {
  const form = document.querySelector('#kad-browser-search');
  if (!form) return;
  form.addEventListener('submit', event => {
    event.preventDefault();
    const number = form.elements.case_number.value.trim().toUpperCase().replace(/^A/, 'А');
    const status = document.querySelector('#kad-browser-status');
    if (!/^[А]\d{1,3}-\d+\/\d{4}$/.test(number) || number.length > 64) {
      status.textContent = 'Проверьте номер дела: например, А99-12345/2099.';
      return;
    }
    if (typeof crypto.randomUUID !== 'function') {
      status.textContent = 'Откройте сервис по HTTPS или на localhost для запуска помощника.';
      return;
    }
    const fragment = new URLSearchParams({sro_robot: '1', case: number, run: crypto.randomUUID()});
    const link = document.createElement('a');
    link.href = `https://kad.arbitr.ru/#${fragment}`;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.click();
    status.textContent = 'Поиск передан в новую вкладку КАД. Дождитесь результата там. Если панель помощника не появилась, проверьте установку расширения.';
  });
})();
