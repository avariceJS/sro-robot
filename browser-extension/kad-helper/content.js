/* UI-only acquisition: normal KAD controls; no server APIs or PDF extraction. */
(() => {
  if (location.pathname !== '/' && !/^\/Card\/[0-9a-f-]+$/i.test(location.pathname)) return;
  const A = SroKadAutomation;
  const P = SroKadProtocol;
  const extensionApi = globalThis.browser || globalThis.chrome;
  const abort = new AbortController();
  let job;
  let panel;
  let status;
  let choices;
  const messages = {
    document_date_missing: 'У части PDF дата акта не подтверждена. Автоматический выбор остановлен.',
    document_date_not_found: 'PDF с указанной датой судебного акта не найден.',
    ambiguous_document_date: 'На указанную дату найдено несколько PDF. Нужен выбор сотрудника.',
    'challenge': 'КАД требует проверку доступа. Помощник остановлен.',
    'rate-limited': 'КАД ограничил запросы. Помощник остановлен без повтора.',
    'gateway-unavailable': 'КАД временно недоступен. Повторная загрузка не помогла, поиск остановлен.',
    'gateway-after-submit': 'КАД показал ошибку сервера после команды «Найти». Поиск не повторялся.',
    'wasm-error': 'Ошибка загрузки модуля КАД. Поиск не завершён.',
    'script-error': 'Ошибка сценария страницы КАД. Поиск не завершён.',
    'timeout': 'КАД не подтвердил результат за отведённое время. Поиск не повторялся.',
    'card-identity': 'Номер или адрес карточки не совпал с запросом. Открытие остановлено.',
    'card-chronology': 'Не удалось определить раскрываемую хронологию для номера дела.',
    'documents-timeout': 'Хронология раскрыта, но КАД не показал в ней доступные PDF-документы.',
    'card-section': 'Не удалось определить раздел «Электронное дело».',
    'search-control': 'Не удалось определить поле или кнопку поиска КАД.',
    'search-filter': 'КАД не закрепил номер в фильтре. Кнопка «Найти» не нажималась.',
    'page-ready': 'Страница КАД не закончила загрузку. Следующий шаг не выполнялся.',
    'aborted': 'Поиск остановлен сотрудником.',
  };
  async function send(type, values = {}) {
    const response = await extensionApi.runtime.sendMessage({type, requestId: job?.requestId, ...values});
    if (!response || response.error) {
      const error = new Error('Помощник остановлен: ' + (response?.error || 'Потеряна связь с помощником.'));
      error.code = response?.code || 'helper_state';
      throw error;
    }
    return response;
  }
  function show(text) {
    if (!panel) {
      panel = document.createElement('section');
      panel.id = 'sro-kad-helper';
      panel.style.cssText = 'position:fixed;right:12px;bottom:12px;z-index:2147483647;width:380px;max-width:90vw;max-height:60vh;overflow:auto;background:white;color:#111;border:2px solid #236799;padding:14px;font:14px/1.5 sans-serif;box-shadow:0 2px 10px #777';
      const title = document.createElement('strong');
      title.textContent = 'СРО Робот — КАД · ' + extensionApi.runtime.getManifest().version;
      status = document.createElement('p');
      status.setAttribute('role', 'status');
      choices = document.createElement('div');
      const stop = document.createElement('button');
      stop.textContent = 'Остановить';
      stop.addEventListener('click', () => {
        abort.abort();
        choices.replaceChildren();
        status.textContent = 'Останавливаю…';
        if (job) send('stop', {reason: 'aborted'})
          .then(() => { status.textContent = messages.aborted; })
          .catch(error => { status.textContent = error.message; });
        else status.textContent = messages.aborted;
      });
      panel.append(title, status, choices, stop);
      document.body.append(panel);
    }
    status.textContent = text;
  }
  const sleep = () => send('tick');
  let opening = false;
  function documentIsCurrent(candidate, automatic) {
    if (abort.signal.aborted || location.href !== job.cardUrl || A.inspectPage(document).state !== 'ok') return false;
    const scopes = A.chronologySections(document, job.caseNumber)
      .filter(section => section.expanded && section.items).map(section => section.items);
    const current = A.collectPdfCandidates(document, job.caseNumber, job.cardUrl, scopes);
    const inventorySignature = items => JSON.stringify(items.map(item => ({
      url: item.url || item.href, label: item.label, actDate: item.actDate || null,
    })).sort((left, right) => left.url.localeCompare(right.url)));
    const sameInventory = inventorySignature(current) === inventorySignature(job.candidates || []);
    const selection = P.automaticDocument(current, job.actDate);
    return sameInventory && (!automatic || (!selection.error && selection.candidate.href === candidate.url)) &&
      current.some(item => item.href === candidate.url && (!job.actDate || item.actDate === job.actDate));
  }
  async function openDocument(candidate, automatic, onReadyToAcquire = null) {
    if (opening) throw Object.assign(new Error('selection_in_progress'), {code: 'invalid_selection'});
    if (abort.signal.aborted) throw Object.assign(new Error('Передача отменена.'), {code: 'aborted'});
    opening = true;
    if (!documentIsCurrent(candidate, automatic)) throw Object.assign(new Error('document_changed'), {code: 'document_changed'});
    // Record the reason before navigating. Ordinary navigation uses the browser's
    // PDF viewer; no download API, blob, file write or download attribute is used.
    await send('select', {url: candidate.url, automatic});
    if (abort.signal.aborted) throw Object.assign(new Error('Передача отменена.'), {code: 'aborted'});
    if (!documentIsCurrent(candidate, automatic)) throw Object.assign(new Error('document_changed'), {code: 'document_changed'});
    if (job.bridge) {
      show('Передаю PDF на локальное распознавание…');
      // Confirm the revalidated choice before the native PDF handoff navigates
      // this content script away. The background owns acquisition and receipt.
      onReadyToAcquire?.();
      await send('acquire');
    } else location.assign(candidate.url);
  }
  async function showDocuments(candidates) {
    if (job.selectedUrl) {
      show('PDF уже открывался в этом запуске. Для нового поиска используйте расширение.');
      return;
    }
    if (job.bridge) {
      show(`В текущей раскрытой хронологии дела ${job.caseNumber} видимых PDF: ${candidates.length}. Полнота списка страниц не подтверждена; проверьте заголовок и дату в сервисе и явно выберите документ для локальной проверки.`);
      return;
    }
    show(`В раскрытой хронологии видно PDF: ${candidates.length}. Полнота списка не подтверждена. Проверьте заголовок и дату и выберите документ для просмотра.`);
    choices.replaceChildren();
    for (const candidate of candidates) {
      const row = document.createElement('p');
      const dateLabel = candidate.actDate ? new Date(`${candidate.actDate}T00:00:00Z`).toLocaleDateString('ru-RU', {timeZone: 'UTC'}) : 'дата не указана';
      const eligible = !job.actDate || candidate.actDate === job.actDate;
      if (!eligible) {
        row.textContent = `${candidate.label} · ${dateLabel} · не соответствует указанной дате`;
        choices.append(row);
        continue;
      }
      const link = document.createElement('a');
      link.textContent = `${candidate.label} · ${dateLabel} · проверить этот PDF`;
      link.href = candidate.url;
      link.addEventListener('click', event => {
        event.preventDefault();
        openDocument(candidate, false).catch(stopWithError);
      });
      row.append(link);
      choices.append(row);
    }
    if (job.actDate && !candidates.some(candidate => candidate.actDate === job.actDate))
      show(`PDF с указанной датой акта ${job.actDate} в видимой хронологии нет. Ничего не выбрано.`);
  }
  extensionApi.runtime.onMessage.addListener((message, _sender, reply) => {
    if (message?.type !== 'service-select') return false;
    const candidate = job?.candidates?.find(item => item.url === message.url);
    if (!job?.bridge || message.requestId !== job.requestId || !candidate || opening ||
        (job.actDate && candidate.actDate !== job.actDate)) {
      reply({error: 'invalid_selection'});
      return false;
    }
    let acknowledged = false;
    openDocument(candidate, false, () => { acknowledged = true; reply({ok: true}); }).catch(error => {
      if (acknowledged) {
        // After acknowledgement, PDF navigation may intentionally destroy this
        // page. Acquisition errors are reported by the background status poll.
        if (abort.signal.aborted) return;
        show('СРО Робот проверяет передачу PDF. Дождитесь результата в списке заданий перед повтором.');
        return;
      }
      stopWithError(error).finally(() => reply({error: error?.code || 'document_changed'}));
    });
    return true;
  });
  async function stopWithError(error) {
    show(messages[error?.code] || (error?.code ? error.message : null) || 'Помощник остановлен: страница или состояние запуска изменились. Проверьте КАД и запустите новый поиск из сервиса.');
    choices.replaceChildren();
    if (job) await send('stop', {reason: error?.code}).catch(() => {});
  }
  async function recoverGateway(error) {
    if (error?.code !== 'gateway-unavailable' || abort.signal.aborted || !job) return false;
    let response;
    try {
      response = await send('gateway-reload');
    } catch (reloadError) {
      if (reloadError.code !== 'gateway-unavailable') throw reloadError;
      show(messages['gateway-unavailable']);
      await send('stop', {reason: 'gateway-unavailable'}).catch(() => {});
      return true;
    }
    const attempt = response.job?.gatewayReloads || 1;
    const limit = response.reloadLimit || 2;
    show(`КАД временно недоступен. Обновляю страницу, попытка ${attempt} из ${limit}…`);
    const delay = Number.isInteger(response.delayMs) ? Math.min(Math.max(response.delayMs, 0), 10000) : 2000;
    for (let i = 0; i < Math.ceil(delay / 250); i++) {
      if (abort.signal.aborted) return true;
      await sleep();
    }
    if (!abort.signal.aborted) location.reload();
    return true;
  }
  async function continueSearch() {
    show(`Ищу дело ${job.caseNumber}…`);
    const result = await A.runSearch(document, job.caseNumber, {
      signal: abort.signal, timeoutMs: 45000, sleep,
      onProgress: stage => show({
        ready: 'Ожидаю загрузку страницы КАД…',
        input: 'Ожидаю поле номера дела…',
        filter: 'Номер введён. Закрепляю его в фильтре КАД…',
        button: 'Номер закреплён в фильтре. Ожидаю кнопку «Найти»…',
        submitted: 'Команда «Найти» отправлена. Ожидаю результат КАД…',
      }[stage]),
    });
    if (result.state !== 'matched') {
      show(result.state === 'no-results' ? 'КАД подтвердил: дел по этому номеру не найдено.' : 'КАД показал неоднозначный результат. Проверьте его вручную.');
      await send('stop');
      return;
    }
    const cardUrl = result.links[0].href;
    await send('card', {cardUrl});
    if (!abort.signal.aborted) location.assign(cardUrl);
  }
  async function main() {
    const params = new URLSearchParams(location.hash.slice(1));
    if (location.pathname === '/' && params.get('sro_robot') === '1') {
      // Remove the handoff from browser history before any navigation.
      history.replaceState(null, '', location.pathname + location.search);
      const started = await send('begin', {caseNumber: params.get('case'), requestId: params.get('run'), actDate: params.get('act_date')});
      job = started.job;
      if (started.replay) throw new Error('interrupted');
      await continueSearch();
      return;
    }
    job = (await send('get')).job;
    if (!job) return;
    if (job.gatewayResume && job.phase === 'searching' && location.pathname === '/' && !location.search) {
      // Only the helper's own reload sets this flag. A manual refresh does not.
      await send('gateway-resumed');
      job = {...job, gatewayResume: false};
      await continueSearch();
      return;
    }
    if (job.phase === 'documents' && location.href === job.cardUrl) {
      await showDocuments(job.candidates);
      return;
    }
    if (job.gatewayResume && job.phase === 'card' && location.href === job.cardUrl)
      await send('gateway-resumed');
    if (job.phase !== 'card' || location.href !== job.cardUrl) {
      show('Предыдущий запуск завершён или прерван. Новый поиск запускается из СРО Робот.');
      return;
    }
    show(`Проверяю карточку ${job.caseNumber} и раскрываю хронологию дела…`);
    const result = await A.runCard(document, job.caseNumber, {signal: abort.signal, cardUrl: job.cardUrl, timeoutMs: 45000, sleep});
    const candidates = result.candidates.map(item => ({url: item.href, label: item.label, ...(item.actDate ? {actDate: item.actDate} : {})}));
    job = (await send('candidates', {candidates, inventoryComplete: result.inventoryComplete})).job;
    if (!abort.signal.aborted) await showDocuments(candidates);
  }
  main().catch(async error => {
    try {
      if (await recoverGateway(error)) return;
    } catch (recoveryError) {
      error = recoveryError;
    }
    await stopWithError(error);
  });
})();
