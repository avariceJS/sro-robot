/* Authenticated same-origin service handoff; no document text enters page storage. */
(() => {
  const SERVICE_ORIGINS = Object.freeze(/* SRO_SERVICE_ORIGINS */['http://localhost:8000', 'http://127.0.0.1:8000']);
  if (!SERVICE_ORIGINS.includes(location.origin) || location.pathname !== '/') return;
  const api = globalThis.browser || globalThis.chrome;
  const form = document.querySelector('#kad-browser-search');
  const status = document.querySelector('#kad-browser-status');
  if (!form || !status) return;
  form.dataset.kadBridge = 'ready';
  form.dataset.kadActDate = 'ready';
  const storageKey = 'sro-kad-pending-v1';
  let pending = null;
  try { pending = JSON.parse(sessionStorage.getItem(storageKey) || 'null'); } catch { /* no stored run */ }
  if (pending && !/^[0-9a-f-]{36}$/i.test(pending.requestId || '')) pending = null;
  let busy = false;
  const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
  const save = () => pending ? sessionStorage.setItem(storageKey, JSON.stringify(pending)) : sessionStorage.removeItem(storageKey);
  const keyFor = id => id.replace(/-/g, '').toLowerCase() + '0000000000000000';
  async function send(type, requestId, extra = {}) {
    const result = await api.runtime.sendMessage({type, requestId, ...extra});
    if (!result || result.error) throw new Error(result?.error || 'Расширение потеряло связь с запуском. Проверьте список заданий перед повтором.');
    return result;
  }
  function taskURL(result) {
    if (!/^\/tasks\/\d+$/.test(result.task_url)) throw new Error('Некорректный ответ сервиса. Проверьте список заданий.');
    return result.task_url;
  }
  async function receipt(key) {
    const response = await fetch(`/kad-helper/receipt/${key}`, {credentials: 'same-origin', redirect: 'error', signal: AbortSignal.timeout(10000)});
    if (response.status === 404) return null;
    if (!response.ok) throw new Error('Не удалось проверить приём. Войдите в сервис и повторите проверку.');
    return taskURL(await response.json());
  }
  async function complete(url) {
    await send('bridge-finish', pending.requestId).catch(() => {});
    pending = null; save();
    location.assign(url);
  }
  const initialized = (async () => {
    const existing = await send('bridge-resume');
    if (!pending && existing.requestId) { pending = {requestId: existing.requestId, importStarted: !!existing.importStarted}; save(); }
    status.textContent = pending
      ? 'Есть незавершённый запуск. Нажмите «Найти и распознать»: сначала проверю его результат, новый поиск не начнётся.'
      : 'Помощник подключён. PDF будет передан на локальное распознавание автоматически.';
  })().catch(() => { status.textContent = 'Не удалось связаться с помощником. Обновите расширение и страницу.'; });
  form.addEventListener('submit', async event => {
    event.preventDefault(); event.stopImmediatePropagation();
    if (busy) return;
    busy = true;
    const button = form.querySelector('button[type=submit]');
    if (button) button.disabled = true;
    try {
      await initialized;
      if (pending) {
        const known = await receipt(keyFor(pending.requestId));
        if (known) { await complete(known); return; }
      } else {
        pending = {requestId: crypto.randomUUID(), importStarted: false}; save();
        await send('bridge-start', pending.requestId, {
          caseNumber: form.elements.case_number.value,
          actDate: form.elements.act_date?.value || null,
        });
      }
      const deadline = Date.now() + 5 * 60 * 1000;
      let result;
      while (Date.now() < deadline) {
        result = await send('bridge-status', pending.requestId);
        if (result.phase === 'ready') break;
        if (result.phase === 'stopped') throw new Error(result.reason || 'КАД не подтвердил документ. Поиск остановлен.');
        status.textContent = result.notice || ({prepared: 'Открываю КАД…', searching: 'Ищу дело в КАД…', card: 'Открываю хронологию дела…', documents: 'Получаю PDF…'})[result.phase] || 'Ожидаю документ…';
        await pause(1000);
      }
      if (result?.phase !== 'ready') throw new Error('Истекло время ожидания. Поиск не повторялся.');
      status.textContent = 'Передаю PDF на локальное распознавание…';
      const binary = atob(result.body);
      const bytes = Uint8Array.from(binary, char => char.charCodeAt(0));
      const body = new FormData();
      body.append('file', new Blob([bytes], {type: 'application/pdf'}), 'kad-document.pdf');
      body.append('idempotency_key', keyFor(pending.requestId));
      const sourceJSON = JSON.stringify(result.source);
      if (new Blob([sourceJSON]).size > 16384) throw new Error('Список документов слишком велик для автоматической передачи. Нужна ручная проверка.');
      body.append('source', sourceJSON);
      await send('bridge-importing', pending.requestId);
      pending.importStarted = true; save();
      const response = await fetch('/kad-helper/import', {method: 'POST', body, credentials: 'same-origin', redirect: 'error', signal: AbortSignal.timeout(90000)});
      if (!response.ok) throw new Error('Приём документа не подтверждён. Повторное нажатие проверит результат и использует тот же ключ.');
      await complete(taskURL(await response.json()));
    } catch (error) {
      status.textContent = error.message || 'Запуск остановлен. Проверьте список заданий.';
      if (pending?.importStarted) {
        // Keep the same run and bytes; a later explicit click resolves/replays only local intake.
        try { const known = await receipt(keyFor(pending.requestId)); if (known) await complete(known); } catch { /* uncertain receipt stays pending */ }
      } else if (pending) {
        await send('bridge-finish', pending.requestId, {keepTab: true}).catch(() => {});
        pending = null; save();
      }
    } finally {
      busy = false;
      if (button) button.disabled = false;
    }
  }, true);
})();
