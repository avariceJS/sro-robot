/* UI-only acquisition: normal KAD controls; no server APIs or PDF extraction. */
(() => {
  if (location.pathname !== '/' && !/^\/Card\/[0-9a-f-]+$/i.test(location.pathname)) return;
  const A = SroKadAutomation;
  const extensionApi = globalThis.browser || globalThis.chrome;
  const abort = new AbortController();
  let job;
  let panel;
  let status;
  let choices;
  const messages = {
    'challenge': 'КАД требует проверку доступа. Помощник остановлен.',
    'rate-limited': 'КАД ограничил запросы. Помощник остановлен без повтора.',
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
    if (!response || response.error) throw new Error('helper_state');
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
        status.textContent = messages.aborted;
        if (job) send('stop').catch(() => {});
      });
      panel.append(title, status, choices, stop);
      document.body.append(panel);
    }
    status.textContent = text;
  }
  let opening = false;
  function documentIsCurrent(candidate, automatic) {
    if (abort.signal.aborted || location.href !== job.cardUrl || A.inspectPage(document).state !== 'ok') return false;
    const scopes = A.chronologySections(document, job.caseNumber)
      .filter(section => section.expanded && section.items).map(section => section.items);
    const current = A.collectPdfCandidates(document, job.caseNumber, job.cardUrl, scopes);
    return (!automatic || current.length === 1) && current.some(item => item.href === candidate.url);
  }
  async function openDocument(candidate, automatic) {
    if (opening || abort.signal.aborted) return;
    opening = true;
    if (!documentIsCurrent(candidate, automatic)) throw new Error('document_changed');
    // Record the reason before navigating. Ordinary navigation uses the browser's
    // PDF viewer; no download API, blob, file write or download attribute is used.
    await send('select', {url: candidate.url, automatic});
    if (abort.signal.aborted) return;
    if (!documentIsCurrent(candidate, automatic)) throw new Error('document_changed');
    location.assign(candidate.url);
  }
  async function showDocuments(candidates) {
    if (job.selectedUrl) {
      show('PDF уже открывался в этом запуске. Для нового поиска используйте расширение.');
      return;
    }
    if (candidates.length === 1) {
      show('Открываю PDF…');
      await openDocument(candidates[0], true);
      return;
    }
    show(`Найдено несколько PDF (${candidates.length}). Выберите документ для просмотра.`);
    choices.replaceChildren();
    for (const candidate of candidates) {
      const row = document.createElement('p');
      const link = document.createElement('a');
      link.textContent = candidate.label;
      link.href = candidate.url;
      link.addEventListener('click', event => {
        event.preventDefault();
        openDocument(candidate, false).catch(stopWithError);
      });
      row.append(link);
      choices.append(row);
    }
  }
  async function stopWithError(error) {
    show(messages[error?.code] || 'Помощник остановлен: страница или состояние запуска изменились. Проверьте КАД и запустите новый поиск из сервиса.');
    choices.replaceChildren();
    if (job) await send('stop').catch(() => {});
  }
  async function main() {
    const params = new URLSearchParams(location.hash.slice(1));
    if (location.pathname === '/' && params.get('sro_robot') === '1') {
      // Remove the handoff from browser history before any navigation.
      history.replaceState(null, '', location.pathname + location.search);
      const started = await send('begin', {caseNumber: params.get('case'), requestId: params.get('run')});
      job = started.job;
      if (started.replay) throw new Error('interrupted');
      show(`Ищу дело ${job.caseNumber}…`);
      const result = await A.runSearch(document, job.caseNumber, {
        signal: abort.signal, timeoutMs: 45000,
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
      return;
    }
    job = (await send('get')).job;
    if (!job) return;
    if (job.phase === 'documents' && location.href === job.cardUrl) {
      await showDocuments(job.candidates);
      return;
    }
    if (job.phase !== 'card' || location.href !== job.cardUrl) {
      show('Предыдущий запуск завершён или прерван. Новый поиск запускается из СРО Робот.');
      return;
    }
    show(`Проверяю карточку ${job.caseNumber} и раскрываю хронологию дела…`);
    const result = await A.runCard(document, job.caseNumber, {signal: abort.signal, cardUrl: job.cardUrl, timeoutMs: 45000});
    const candidates = result.candidates.map(item => ({url: item.href, label: item.label}));
    await send('candidates', {candidates});
    if (!abort.signal.aborted) await showDocuments(candidates);
  }
  main().catch(stopWithError);
})();
