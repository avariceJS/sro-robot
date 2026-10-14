if (typeof importScripts === 'function') { importScripts('protocol.js'); importScripts('native-pdf.js'); importScripts('bridge.js'); }
const P = globalThis.SroKadProtocol;
const extensionApi = globalThis.browser || globalThis.chrome;
const TTL = 30 * 60 * 1000;
const GATEWAY_RELOAD_LIMIT = 2;
const GATEWAY_RELOAD_DELAYS_MS = [2000, 6000];
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
  'gateway-unavailable': 'КАД временно недоступен. Повторная загрузка страницы не помогла.',
  'gateway-after-submit': 'КАД показал ошибку сервера после команды «Найти». Поиск не повторялся.',
  'wasm-error': 'Модуль КАД не запустился. Поиск остановлен.',
  'script-error': 'Ошибка сценария КАД. Поиск остановлен.',
  ambiguous_documents: 'В деле несколько PDF. Нужен выбор документа через обычный режим расширения.',
  document_date_missing: 'У части PDF не удалось подтвердить дату акта. Автоматический выбор остановлен.',
  document_date_not_found: 'PDF с указанной датой судебного акта не найден.',
  ambiguous_document_date: 'На эту дату найдено несколько PDF. Нужен выбор сотрудника.',
  import_started: 'Передача в локальный сервис уже началась. Проверьте результат в списке заданий.',
  native_pdf_permission: 'Включите передачу PDF в меню помощника и повторите запуск.',
  native_pdf_unavailable: 'Для автоматической передачи PDF нужен обновлённый помощник Firefox или Chrome.',
  pdf_debug_detached: 'Chrome прервал чтение ответа PDF. Передача остановлена.',
  document_changed: 'Ссылка или дата выбранного PDF изменилась. Открытие остановлено.',
  invalid_auto_selection: 'Не удалось подтвердить единственный PDF по указанной дате.',
  invalid_acquisition: 'Выбор PDF не совпал с состоянием передачи.',
  invalid_transition: 'Состояние карточки изменилось перед передачей PDF.',
  invalid_pdf: 'Адрес документа не прошёл проверку принадлежности карточке.',
  invalid_act_date: 'Дата судебного акта имеет некорректный формат.',
  invalid_service: 'Не удалось подтвердить адрес вкладки локального сервиса.',
  service_left: 'Вкладка локального сервиса закрыта или её адрес изменился.',
  run_stopped: 'Вкладка КАД закрыта. Передача документа не выполнена; запустите новый поиск.',
  aborted: 'Передача PDF отменена. Повтор автоматически не выполнялся.',
  acquisition_cancelled: 'Передача PDF отменена: связь с запуском изменилась.',
  pdf_navigation: 'Браузер не смог открыть вкладку PDF.',
  pdf_stream: 'Не удалось прочитать поток PDF в Firefox.',
  pdf_capture: 'Не удалось начать передачу PDF.',
  pdf_duplicate_request: 'Запрос PDF изменился во время передачи.',
  pdf_duplicate_response: 'КАД вернул повторный ответ PDF. Передача остановлена.',
  pdf_network: 'Браузер не смог получить PDF: ошибка сетевого запроса.',
  pdf_timeout: 'Истекло время получения PDF. Повтор автоматически не выполнялся.',
  pdf_http: 'Сервер КАД вернул ошибку при получении PDF.',
  pdf_post_redirect: 'Выдача PDF перенаправлена на другой запрос. Передача остановлена.',
  pdf_redirect: 'КАД перенаправил PDF на неподтверждённый адрес.',
  pdf_navigation_mismatch: 'Вкладка перешла на другой адрес во время получения PDF. Передача остановлена.',
  pdf_handoff_mismatch: 'Последовательность выдачи PDF изменилась. Передача остановлена без повтора.',
  pdf_handoff_too_large: 'Промежуточная страница PDF превышает допустимый размер.',
  pdf_handoff_malformed: 'КАД вернул некорректную промежуточную страницу PDF.',
  pdf_handoff_unsupported_parser: 'Firefox не смог проверить промежуточную страницу PDF.',
  pdf_handoff_malformed_document: 'КАД вернул некорректную промежуточную страницу PDF.',
  pdf_handoff_missing_form: 'КАД вернул HTML без подтверждённой формы выдачи PDF. Вкладка сохранена.',
  pdf_handoff_multiple_forms: 'На промежуточной странице несколько форм. Передача PDF остановлена.',
  pdf_handoff_method_mismatch: 'Форма выдачи PDF изменилась. Автоматическая передача остановлена.',
  pdf_handoff_action_mismatch: 'Форма выдачи PDF ведёт на неподтверждённый адрес. Передача остановлена.',
  pdf_handoff_interactive_control: 'Структура промежуточной страницы PDF не подтверждена. Вкладка сохранена.',
  pdf_handoff_unsupported_fields: 'Поля формы выдачи PDF изменились. Передача остановлена.',
  pdf_handoff_missing_hidden_fields: 'КАД не предоставил подтверждённую форму выдачи PDF.',
  pdf_html_viewer: 'Firefox передал HTML своего просмотрщика вместо исходных байтов PDF. Вкладка сохранена.',
  pdf_html_challenge: 'Перед PDF получена страница проверки доступа КАД. Автоматизация остановлена; вкладка сохранена.',
  pdf_html_original: 'Ссылка судебного акта вернула промежуточную HTML-страницу. Передача остановлена; вкладка сохранена.',
  pdf_html_final: 'Конечный адрес документа вернул HTML вместо PDF. Передача остановлена; вкладка сохранена.',
  pdf_html: 'Браузер передал HTML вместо исходного PDF. Автоматическая передача остановлена; вкладка сохранена.',
  pdf_headers_missing: 'Браузер не подтвердил заголовки исходного PDF. Передача остановлена.',
  pdf_mime: 'КАД вернул документ с неожиданным типом содержимого.',
  pdf_viewer_shell: 'Chrome открыл PDF в просмотрщике, но помощник получил страницу просмотрщика вместо файла. Передача остановлена; вкладка сохранена.',
  not_pdf: 'Ответ КАД не содержит PDF-документ.',
  pdf_too_large: 'PDF превышает допустимый размер.',
  helper_state: 'Потеряна связь с состоянием запуска помощника.',
  timeout: 'КАД не подтвердил результат вовремя. Поиск не повторялся.',
  'search-filter': 'КАД не закрепил номер в фильтре. Кнопка «Найти» не нажималась.',
  'page-ready': 'Элементы страницы КАД не стали готовы вовремя. Поиск не отправлен.',
  'card-identity': 'Номер или адрес карточки не совпал с запросом.',
  'card-chronology': 'Не удалось раскрыть хронологию дела.',
  'documents-timeout': 'В раскрытой хронологии не появились доступные PDF.',
};

function onGatewayPage(phase, page, cardUrl) {
  if (phase === 'searching') return page.pathname === '/' && !page.search;
  if (phase !== 'card') return false;
  try { return P.cardURL(page.href) === cardUrl; }
  catch { return false; }
}
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
    job = {...(job?.phase === 'prepared' ? job : {}), requestId: id, caseNumber: number, actDate, phase: 'searching', startedAt: Date.now(), gatewayReloads: 0, gatewayResume: false};
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
      job = {...job, phase: 'card', cardUrl: P.cardURL(message.cardUrl), gatewayReloads: 0, gatewayResume: false};
    } else if (message.type === 'gateway-reload') {
      // Same-document reload only. This does not repeat a submitted search and
      // does not apply to an access check or a rate limit.
      if (!onGatewayPage(job.phase, url, job.cardUrl)) throw new Error('invalid_transition');
      const attempts = Number.isInteger(job.gatewayReloads) && job.gatewayReloads > 0 ? job.gatewayReloads : 0;
      if (attempts >= GATEWAY_RELOAD_LIMIT) throw new Error('gateway-unavailable');
      job = {...job, gatewayReloads: attempts + 1, gatewayResume: true};
      await extensionApi.storage.session.set({[key]: job});
      return {job, delayMs: GATEWAY_RELOAD_DELAYS_MS[attempts], reloadLimit: GATEWAY_RELOAD_LIMIT};
    } else if (message.type === 'gateway-resumed') {
      if (!job.gatewayResume || !onGatewayPage(job.phase, url, job.cardUrl)) throw new Error('invalid_transition');
      job = {...job, gatewayResume: false};
    } else if (message.type === 'stop') {
      if (SroKadBridge.importStarted(sender.tab.id, message.requestId)) throw new Error('import_started');
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
      try { job = await SroKadBridge.acquire(job, sender.tab.id); }
      catch (error) {
        const code = Object.hasOwn(stopReasons, error?.message) ? error.message : 'helper_state';
        job = {...job, phase: 'stopped', reason: stopReasons[code]};
        await extensionApi.storage.session.set({[key]: job});
        throw error;
      }
    } else throw new Error('invalid_message');
  }
  await extensionApi.storage.session.set({[key]: job});
  return {job};
}

extensionApi.runtime.onMessage.addListener((message, sender, reply) => {
  const id = sender.tab?.id;
  // Cancellation must reach an in-flight capture before waiting for its tab lock.
  if (message.type === 'stop' && sender.id === extensionApi.runtime.id &&
      Number.isInteger(id) && sender.frameId === 0)
    SroKadBridge.cancel(id, message.requestId, sender.url);
  withLock(id, () => handle(message, sender)).then(reply, error => {
    const code = Object.hasOwn(stopReasons, error?.message) ? error.message : 'helper_state';
    reply({error: stopReasons[code], code});
  });
  return true;
});
extensionApi.tabs.onRemoved.addListener(tabId => withLock(tabId, () => extensionApi.storage.session.remove(keyFor(tabId))));
