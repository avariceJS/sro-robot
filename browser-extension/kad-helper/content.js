/* UI-only acquisition: normal KAD controls; no server APIs or PDF extraction. */
(() => {
  if (location.pathname !== '/' && !/^\/Card\/[0-9a-f-]+$/i.test(location.pathname)) return;
  const A = SroKadAutomation;
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
    'card-section': 'Не удалось определить раздел «Электронное дело».',
    'search-control': 'Не удалось определить поле или кнопку поиска КАД.',
    'search-filter': 'КАД не подтвердил введённый номер дела.',
    'aborted': 'Поиск остановлен сотрудником.',
  };
  async function send(type, values = {}) {
    const response = await chrome.runtime.sendMessage({type, requestId: job?.requestId, ...values});
    if (!response || response.error) throw new Error('helper_state');
    return response;
  }
  function show(text) {
    if (!panel) {
      panel = document.createElement('section');
      panel.id = 'sro-kad-helper';
      panel.style.cssText = 'position:fixed;right:12px;bottom:12px;z-index:2147483647;width:380px;max-width:90vw;max-height:60vh;overflow:auto;background:white;color:#111;border:2px solid #236799;padding:14px;font:14px/1.5 sans-serif;box-shadow:0 2px 10px #777';
      const title = document.createElement('strong');
      title.textContent = 'СРО Робот — КАД';
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
  function showDocuments(candidates) {
    show(`Дело ${job.caseNumber} открыто. Выберите нужный судебный акт (${candidates.length}). Тип документа автоматически не определяется.`);
    choices.replaceChildren();
    for (const candidate of candidates) {
      const row = document.createElement('p');
      const link = document.createElement('a');
      link.textContent = candidate.label;
      link.href = candidate.url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.addEventListener('click', event => {
        if (abort.signal.aborted) { event.preventDefault(); return; }
        let current = {state: 'invalid'};
        let stillVisible = false;
        try {
          current = A.inspectPage(document);
          stillVisible = A.collectPdfCandidates(document, job.caseNumber, job.cardUrl)
            .some(item => item.href === candidate.url);
        } catch { /* Changed or oversized inventory stops navigation below. */ }
        if (current.state !== 'ok' || !stillVisible) {
          event.preventDefault();
          abort.abort();
          choices.replaceChildren();
          show('Страница изменилась или КАД ограничил доступ. Открытие документа остановлено.');
          send('stop').catch(() => {});
          return;
        }
        // Metadata is retained locally; this click only opens the source's PDF link.
        send('select', {url: candidate.url}).catch(() => show('Не удалось сохранить сведения о выборе. PDF открыт; проверьте документ перед загрузкой в сервис.'));
      });
      row.append(link);
      choices.append(row);
    }
    const note = document.createElement('p');
    note.textContent = 'Сохраните PDF и загрузите в СРО Робот для локального распознавания.';
    choices.append(note);
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
      const result = await A.runSearch(document, job.caseNumber, {signal: abort.signal, timeoutMs: 45000});
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
      showDocuments(job.candidates);
      return;
    }
    if (job.phase !== 'card' || location.href !== job.cardUrl) {
      show('Предыдущий запуск завершён или прерван. Новый поиск запускается из СРО Робот.');
      return;
    }
    show(`Проверяю карточку ${job.caseNumber} и открываю электронное дело…`);
    const result = await A.runCard(document, job.caseNumber, {signal: abort.signal, cardUrl: job.cardUrl, timeoutMs: 30000});
    const candidates = result.candidates.map(item => ({url: item.href, label: item.label}));
    await send('candidates', {candidates});
    if (!abort.signal.aborted) showDocuments(candidates);
  }
  main().catch(async error => {
    show(messages[error?.code] || 'Помощник остановлен: страница или состояние запуска изменились. Проверьте КАД и запустите новый поиск из сервиса.');
    choices.replaceChildren();
    if (job) await send('stop').catch(() => {});
  });
})();
