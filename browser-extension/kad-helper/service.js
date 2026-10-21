/* Authenticated same-origin service handoff; no document text enters page storage. */
(() => {
  const SERVICE_ORIGINS = Object.freeze(/* SRO_SERVICE_ORIGINS */['http://localhost:8000', 'http://127.0.0.1:8000']);
  const SERVICE_PATHS = new Set(['/', '/debtors/intake', '/kad-helper']);
  if (!SERVICE_ORIGINS.includes(location.origin) || !SERVICE_PATHS.has(location.pathname)) return;
  function supportedVersion(value) {
    if (typeof value !== 'string' || !/^\d+\.\d+\.\d+$/.test(value)) return false;
    const [major, minor, patch] = value.split('.').map(Number);
    return major > 0 || minor > 3 || minor === 3 && patch >= 0;
  }
  const api = globalThis.browser || globalThis.chrome;
  const form = document.querySelector('#kad-browser-search');
  const status = document.querySelector('#kad-browser-status');
  const connectionStatus = document.querySelector('#kad-helper-connection-status');
  const choices = document.querySelector('#kad-candidate-choices');
  if (!form) {
    if (!connectionStatus) return;
    const checkConnection = () => api.runtime.sendMessage({type: 'bridge-capabilities'}).then(result => {
      const required = ['https-origin', 'explicit-document-choice', 'receipt-reconciliation'];
      const compatible = result?.protocol === 1 && supportedVersion(result.version) && required.every(item => result.capabilities?.includes(item));
      connectionStatus.textContent = !compatible
        ? 'Установленная версия помощника несовместима. Поиск отключён до установки актуальной версии.'
        : result.pdfPermissionReady
          ? 'Помощник подключён и готов к поиску и передаче выбранного PDF.'
          : result.pdfPermissionMode === 'user-enable'
            ? 'Помощник подключён. Откройте меню расширения и включите передачу PDF, затем вернитесь сюда.'
            : 'Помощник подключён, но браузер не подтвердил доступ для передачи PDF. Проверьте доступ расширения в настройках браузера.';
      connectionStatus.dataset.state = !compatible ? 'unavailable' : result.pdfPermissionReady ? 'ready' : 'permission';
    }).catch(() => {
      connectionStatus.textContent = 'Не удалось связаться с помощником. Перезагрузите страницу и проверьте установку.';
      connectionStatus.dataset.state = 'unavailable';
    });
    checkConnection();
    window.addEventListener('focus', checkConnection);
    return;
  }
  if (!status || !choices) return;
  const submitButton = form.querySelector('button[type="submit"]');
  if (submitButton) submitButton.disabled = true;
  const storageKey = 'sro-kad-pending-v1';
  let pending = null;
  try { pending = JSON.parse(sessionStorage.getItem(storageKey) || 'null'); } catch { /* no stored run */ }
  if (pending && !/^[0-9a-f-]{36}$/i.test(pending.requestId || '')) pending = null;
  let busy = false;
  let candidateSignature = '';
  let candidateError = null;
  const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
  const save = () => pending ? sessionStorage.setItem(storageKey, JSON.stringify(pending)) : sessionStorage.removeItem(storageKey);
  const keyFor = id => id.replace(/-/g, '').toLowerCase() + '0000000000000000';
  async function send(type, requestId, extra = {}) {
    let result;
    try {
      result = await api.runtime.sendMessage({type, requestId, ...extra});
    } catch {
      throw new Error('Связь с помощником потеряна. Обновите страницу и сначала проверьте список заданий перед повтором.');
    }
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
  function showCandidateChoices(result) {
    const candidates = Array.isArray(result.candidates) ? result.candidates : [];
    const signature = JSON.stringify([result.caseNumber, result.requestedDate, candidates]);
    if (signature === candidateSignature) return;
    candidateSignature = signature;
    choices.replaceChildren();
    const intro = document.createElement('p');
    intro.className = 'mb-3 text-sm leading-6 text-slate-600';
    intro.textContent = `В раскрытой хронологии дела ${result.caseNumber} показано видимых PDF: ${candidates.length}. Полнота списка страниц не подтверждена. Название и дата не устанавливают тип судебного акта. Проверьте строку и явно подтвердите документ для локальной проверки.`;
    choices.append(intro);
    for (const candidate of candidates) {
      const row = document.createElement('article');
      row.className = 'kad-candidate-choice';
      const heading = document.createElement('div');
      heading.className = 'kad-candidate-heading';
      const title = document.createElement('h3');
      title.className = 'kad-candidate-title';
      title.textContent = candidate.label;
      const date = document.createElement('span');
      date.className = 'kad-candidate-date';
      const shownDate = candidate.actDate ? new Date(`${candidate.actDate}T00:00:00Z`).toLocaleDateString('ru-RU', {timeZone: 'UTC'}) : 'Дата не указана';
      date.textContent = shownDate;
      const button = document.createElement('button');
      button.className = 'kad-candidate-action';
      button.type = 'button';
      button.textContent = 'Выбрать этот PDF для распознавания';
      button.disabled = !!result.requestedDate && candidate.actDate !== result.requestedDate;
      button.addEventListener('click', async () => {
        if (button.disabled) return;
        for (const other of choices.querySelectorAll('button')) other.disabled = true;
        status.textContent = `Вы выбрали «${candidate.label}». Проверяю ссылку в открытой карточке дела и получаю PDF…`;
        try {
          await send('bridge-select', pending.requestId, {url: candidate.url});
        } catch (error) {
          candidateError = error;
          status.textContent = error.message || 'Выбор не подтверждён. Запустите новый поиск.';
          choices.replaceChildren();
        }
      });
      heading.append(title, date);
      row.append(heading, button);
      choices.append(row);
    }
    if (result.requestedDate && !candidates.some(item => item.actDate === result.requestedDate)) {
      const note = document.createElement('p');
      note.className = 'rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm leading-6 text-amber-900';
      note.setAttribute('role', 'alert');
      note.textContent = 'Среди видимых документов нет PDF с указанной датой акта. Ничего не выбрано.';
      choices.append(note);
    }
  }
  let initialized = Promise.resolve();
  let checking = null;
  async function checkConnection() {
    if (checking) return checking;
    checking = (async () => {
    const capabilities = await send('bridge-capabilities');
    const requiredCapabilities = ['https-origin', 'explicit-document-choice', 'receipt-reconciliation'];
    if (capabilities.protocol !== 1 || !supportedVersion(capabilities.version) ||
        !requiredCapabilities.every(item => capabilities.capabilities?.includes(item))) {
      throw new Error('Установленная версия помощника несовместима. Поиск отключён до установки актуальной версии.');
    }
    if (!capabilities.pdfPermissionReady) {
      form.dataset.kadBridge = 'unavailable';
      status.textContent = capabilities.pdfPermissionMode === 'user-enable'
        ? 'Помощник подключён, но передача PDF отключена. Откройте меню расширения и включите передачу PDF.'
        : 'Помощник подключён, но браузер не подтвердил доступ для передачи PDF. Проверьте настройки расширения.';
      status.dataset.state = 'permission';
      if (submitButton) submitButton.disabled = true;
      return;
    }
    form.dataset.kadBridge = 'ready';
    form.dataset.kadActDate = 'ready';
    form.dataset.kadProtocol = '1';
    const existing = await send('bridge-resume');
    if (!pending && existing.requestId) {
      pending = {requestId: existing.requestId, importStarted: !!existing.importStarted, reprocess: false};
      save();
    }
    const reprocessControl = form.elements.kad_reprocess;
    if (reprocessControl && pending) {
      reprocessControl.checked = pending.reprocess === true;
      reprocessControl.disabled = true;
    }
    status.textContent = pending
      ? 'Есть незавершённый запуск. Нажмите «Найти и распознать»: сначала проверю его результат, новый поиск не начнётся.'
      : 'Помощник подключён и совместим. После явного выбора PDF он будет передан на локальное распознавание.';
    status.dataset.state = 'ready';
    if (submitButton) submitButton.disabled = false;
    })().catch(error => {
    form.dataset.kadBridge = 'unavailable';
    status.textContent = error?.message || 'Помощник не подключён. Перезагрузите страницу и проверьте установку.';
    status.dataset.state = 'unavailable';
    if (submitButton) submitButton.disabled = true;
    }).finally(() => { checking = null; });
    initialized = checking;
    return checking;
  }
  initialized = checkConnection();
  window.addEventListener('focus', () => {
    if (!busy && form.dataset.kadBridge !== 'ready') initialized = checkConnection();
  });
  form.addEventListener('submit', async event => {
    event.preventDefault(); event.stopImmediatePropagation();
    if (busy) return;
    busy = true;
    const button = form.querySelector('button[type=submit]');
    if (button) button.disabled = true;
    candidateSignature = '';
    candidateError = null;
    choices.replaceChildren();
    try {
      await initialized;
      if (form.dataset.kadBridge !== 'ready') throw new Error(status.textContent || 'Помощник не подключён или несовместим. Настройте его и повторите попытку.');
      if (pending) {
        const known = await receipt(keyFor(pending.requestId));
        if (known) { await complete(known); return; }
      } else {
        pending = {
          requestId: crypto.randomUUID(),
          importStarted: false,
          reprocess: form.elements.kad_reprocess?.checked === true,
        };
        if (form.elements.kad_reprocess) form.elements.kad_reprocess.disabled = true;
        save();
        await send('bridge-start', pending.requestId, {
          caseNumber: form.elements.case_number.value,
          actDate: form.elements.act_date?.value || null,
        });
      }
      const deadline = Date.now() + 5 * 60 * 1000;
      let result;
      while (Date.now() < deadline) {
        if (candidateError) throw candidateError;
        result = await send('bridge-status', pending.requestId);
        if (result.phase === 'ready') break;
        if (result.phase === 'stopped') throw new Error(result.reason || 'КАД не подтвердил документ. Поиск остановлен.');
        if (result.phase === 'documents') {
          status.textContent = 'Показаны видимые PDF из хронологии. Подтвердите документ для обработки.';
          showCandidateChoices(result);
          if (!(result.candidates || []).length) {
            await send('bridge-finish', pending.requestId, {keepTab: true});
            pending = null; save();
            throw new Error('В раскрытой хронологии не видно PDF. Полнота списка не подтверждена; ничего не импортировано. Проверьте страницу КАД и при необходимости запустите новый поиск.');
          }
          if (result.requestedDate && !(result.candidates || []).some(item => item.actDate === result.requestedDate)) {
            await send('bridge-finish', pending.requestId, {keepTab: true});
            pending = null; save();
            throw new Error('В видимой хронологии нет PDF с указанной датой. Ничего не импортировано; проверьте дату или запустите новый поиск.');
          }
          await pause(750);
          continue;
        }
        if (result.phase === 'acquiring') status.textContent = 'Выбор принят. Получаю PDF и проверяю его перед локальной обработкой…';
        status.textContent = result.notice || ({prepared: 'Открываю КАД…', searching: 'Ищу дело в КАД…', card: 'Открываю хронологию дела…', acquiring: 'Выбор принят. Получаю PDF…'})[result.phase] || 'Ожидаю документ…';
        await pause(1000);
      }
      if (result?.phase !== 'ready') throw new Error('Истекло время ожидания. Поиск не повторялся.');
      status.textContent = 'Передаю PDF на локальное распознавание…';
      const binary = atob(result.body);
      const bytes = Uint8Array.from(binary, char => char.charCodeAt(0));
      const body = new FormData();
      body.append('file', new Blob([bytes], {type: 'application/pdf'}), 'kad-document.pdf');
      body.append('idempotency_key', keyFor(pending.requestId));
      body.append('reprocess', pending.reprocess === true ? 'true' : 'false');
      const sourceJSON = JSON.stringify(result.source);
      if (new Blob([sourceJSON]).size > 16384) throw new Error('Список документов слишком велик для автоматической передачи. Нужна ручная проверка.');
      body.append('source', sourceJSON);
      await send('bridge-importing', pending.requestId);
      pending.importStarted = true; save();
      const response = await fetch('/kad-helper/import', {method: 'POST', body, credentials: 'same-origin', redirect: 'error', signal: AbortSignal.timeout(90000)});
      if (!response.ok) {
        const payload = await response.json().catch(() => null);
        const detail = payload?.detail;
        if (response.status === 409 && detail?.code === 'kad_source_conflict') {
          await send('bridge-finish', pending.requestId, {keepTab: true}).catch(() => {});
          pending = null;
          save();
          if (form.elements.kad_reprocess) form.elements.kad_reprocess.disabled = false;
          throw new Error(detail.message || 'Выбранный источник уже обрабатывается. Проверьте список заданий.');
        }
        throw new Error('Приём документа не подтверждён. Повторное нажатие проверит результат и использует тот же ключ.');
      }
      await complete(taskURL(await response.json()));
      choices.replaceChildren();
    } catch (error) {
      status.textContent = error.message || 'Запуск остановлен. Проверьте список заданий.';
      candidateSignature = '';
      choices.replaceChildren();
      if (pending?.importStarted) {
        // Keep the same run and bytes; a later explicit click resolves/replays only local intake.
        try { const known = await receipt(keyFor(pending.requestId)); if (known) await complete(known); } catch { /* uncertain receipt stays pending */ }
      } else if (pending) {
        await send('bridge-finish', pending.requestId, {keepTab: true}).catch(() => {});
        pending = null; save();
        if (form.elements.kad_reprocess) form.elements.kad_reprocess.disabled = false;
      }
    } finally {
      busy = false;
      if (button) button.disabled = false;
    }
  }, true);
})();
