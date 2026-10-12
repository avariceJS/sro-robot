if (typeof importScripts === 'function') { importScripts('protocol.js'); importScripts('bridge.js'); }
const P = globalThis.SroKadProtocol;
const extensionApi = globalThis.browser || globalThis.chrome;
const TTL = 30 * 60 * 1000;
const locks = new Map();
const keyFor = tabId => `kad:${tabId}`;
function withLock(id, action) {
  const pending = (locks.get(id) || Promise.resolve()).catch(() => {}).then(action);
  locks.set(id, pending);
  pending.finally(() => { if (locks.get(id) === pending) locks.delete(id); }).catch(() => {});
  return pending;
}
globalThis.SroKadWithLock = withLock;
const stopReasons = {
  challenge: 'КАД требует проверку доступа. Автоматический повтор запрещён.',
  'rate-limited': 'КАД ограничил запросы (429). Автоматический повтор запрещён.',
  'wasm-error': 'Модуль КАД не запустился. Поиск остановлен.',
  'script-error': 'Ошибка сценария КАД. Поиск остановлен.',
  ambiguous_documents: 'В деле несколько PDF. Нужен выбор документа через обычный режим расширения.',
  document_date_missing: 'У части PDF не удалось подтвердить дату акта. Автоматический выбор остановлен.',
  document_date_not_found: 'PDF с указанной датой судебного акта не найден.',
  ambiguous_document_date: 'На эту дату найдено несколько PDF. Нужен выбор сотрудника.',
  timeout: 'КАД не подтвердил результат вовремя. Поиск не повторялся.',
  'search-filter': 'КАД не закрепил номер в фильтре. Кнопка «Найти» не нажималась.',
  'page-ready': 'Элементы страницы КАД не стали готовы вовремя. Поиск не отправлен.',
  'card-identity': 'Номер или адрес карточки не совпал с запросом.',
  'card-chronology': 'Не удалось раскрыть хронологию дела.',
  'documents-timeout': 'В раскрытой хронологии не появились доступные PDF.',
};

async function handle(message, sender) {
  if (sender.id !== extensionApi.runtime.id || !Number.isInteger(sender.tab?.id) || sender.frameId !== 0) throw new Error('invalid_sender');
  if (message.type?.startsWith('bridge-')) return SroKadBridge.receive(message, sender);
  const url = P.kadURL(sender.url);
  const key = keyFor(sender.tab.id);
  const saved = (await extensionApi.storage.session.get(key))[key];
  let job = saved && Date.now() - saved.startedAt < TTL ? saved : null;
  if (saved && !job) await extensionApi.storage.session.remove(key);
  if (message.type === 'begin') {
    if (url.pathname !== '/' || url.search) throw new Error('invalid_search_page');
    const id = P.requestId(message.requestId);
    if (job?.requestId === id && job.phase !== 'prepared') return {job, replay: true};
    const number = P.normalizeCase(message.caseNumber);
    const actDate = P.actDate(message.actDate);
    if (job?.bridge && (job.requestId !== id || job.caseNumber !== number || (job.actDate || null) !== actDate)) throw new Error('invalid_prepared_job');
    job = {...(job?.phase === 'prepared' ? job : {}), requestId: id, caseNumber: number, actDate, phase: 'searching', startedAt: Date.now()};
  } else if (message.type === 'get') {
    return {job};
  } else {
    if (!job || message.requestId !== job.requestId) throw new Error('expired_job');
    if (message.type === 'tick') {
      if (!['searching', 'card'].includes(job.phase) ||
          (job.phase === 'searching' ? url.pathname !== '/' || !!url.search : P.cardURL(url.href) !== job.cardUrl))
        throw new Error('invalid_tick');
      // A short live message avoids Firefox's page-load deferral of content
      // timers. This is not a durable scheduler; a lost worker stops the run.
      await new Promise(resolve => setTimeout(resolve, 250));
      return {ok: true};
    } else if (message.type === 'card') {
      if (job.phase !== 'searching' || url.pathname !== '/') throw new Error('invalid_transition');
      job = {...job, phase: 'card', cardUrl: P.cardURL(message.cardUrl)};
    } else if (message.type === 'stop') {
      job = {...job, phase: 'stopped', reason: stopReasons[message.reason] || 'КАД не подтвердил документ или запуск остановлен. Откройте обычный режим расширения для проверки.'};
    } else if (message.type === 'candidates') {
      if (job.phase !== 'card' || P.cardURL(url.href) !== job.cardUrl) throw new Error('invalid_transition');
      if (!Array.isArray(message.candidates) || !message.candidates.length || message.candidates.length > 100) throw new Error('invalid_candidates');
      const cardId = new URL(job.cardUrl).pathname.split('/')[2].toLowerCase();
      const candidates = message.candidates.map(item => {
        const pdf = P.kadURL(item.url);
        const documentId = '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}';
        if (!new RegExp(`^/Kad/PdfDocument/${cardId}/${documentId}/[^/]+\\.pdf$`, 'i').test(pdf.pathname) || pdf.search || pdf.hash) throw new Error('invalid_pdf');
        if (typeof item.label !== 'string' || !item.label.trim() || item.label.length > 2000) throw new Error('invalid_label');
        return {url: pdf.href, label: item.label, ...(P.actDate(item.actDate) ? {actDate: item.actDate} : {})};
      });
      if (new Set(candidates.map(item => item.url)).size !== candidates.length) throw new Error('duplicate_candidates');
      job = {...job, phase: 'documents', candidates};
    } else if (message.type === 'select') {
      if (job.phase !== 'documents' || P.cardURL(url.href) !== job.cardUrl || !job.candidates.some(item => item.url === message.url)) throw new Error('invalid_selection');
      const selection = P.automaticDocument(job.candidates, job.actDate);
      if (message.automatic && (selection.error || selection.candidate.url !== message.url || job.selectedUrl)) throw new Error('invalid_auto_selection');
      if (!message.automatic && job.actDate && !job.candidates.some(item => item.url === message.url && item.actDate === job.actDate)) throw new Error('invalid_selection');
      job = {...job, selectedUrl: message.url, selectionReason: message.automatic ? selection.reason : 'employee_selected_visible_document'};
    } else if (message.type === 'acquire') {
      if (P.cardURL(url.href) !== job.cardUrl) throw new Error('invalid_acquisition_page');
      job = await SroKadBridge.acquire(job, sender.tab.id);
    } else throw new Error('invalid_message');
  }
  await extensionApi.storage.session.set({[key]: job});
  return {job};
}

extensionApi.runtime.onMessage.addListener((message, sender, reply) => {
  const id = sender.tab?.id;
  withLock(id, () => handle(message, sender)).then(reply, () => reply({error: 'Помощник остановлен: состояние вкладки изменилось. Запустите новый поиск из сервиса.'}));
  return true;
});
extensionApi.tabs.onRemoved.addListener(tabId => withLock(tabId, () => extensionApi.storage.session.remove(keyFor(tabId))));
