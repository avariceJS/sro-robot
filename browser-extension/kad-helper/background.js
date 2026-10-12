if (typeof importScripts === 'function') importScripts('protocol.js');
const P = globalThis.SroKadProtocol;
const extensionApi = globalThis.browser || globalThis.chrome;
const TTL = 30 * 60 * 1000;
const locks = new Map();
const keyFor = tabId => `kad:${tabId}`;

async function handle(message, sender) {
  if (sender.id !== extensionApi.runtime.id || !Number.isInteger(sender.tab?.id) || sender.frameId !== 0) throw new Error('invalid_sender');
  const url = P.kadURL(sender.url);
  const key = keyFor(sender.tab.id);
  const saved = (await extensionApi.storage.session.get(key))[key];
  let job = saved && Date.now() - saved.startedAt < TTL ? saved : null;
  if (saved && !job) await extensionApi.storage.session.remove(key);
  if (message.type === 'begin') {
    if (url.pathname !== '/' || url.search) throw new Error('invalid_search_page');
    const id = P.requestId(message.requestId);
    if (job?.requestId === id) return {job, replay: true};
    job = {requestId: id, caseNumber: P.normalizeCase(message.caseNumber), phase: 'searching', startedAt: Date.now()};
  } else if (message.type === 'get') {
    return {job};
  } else {
    if (!job || message.requestId !== job.requestId) throw new Error('expired_job');
    if (message.type === 'card') {
      if (job.phase !== 'searching' || url.pathname !== '/') throw new Error('invalid_transition');
      job = {...job, phase: 'card', cardUrl: P.cardURL(message.cardUrl)};
    } else if (message.type === 'stop') {
      job = {...job, phase: 'stopped', reason: 'operator_or_acquisition_stop'};
    } else if (message.type === 'candidates') {
      if (job.phase !== 'card' || P.cardURL(url.href) !== job.cardUrl) throw new Error('invalid_transition');
      if (!Array.isArray(message.candidates) || !message.candidates.length || message.candidates.length > 100) throw new Error('invalid_candidates');
      const cardId = new URL(job.cardUrl).pathname.split('/')[2].toLowerCase();
      const candidates = message.candidates.map(item => {
        const pdf = P.kadURL(item.url);
        const documentId = '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}';
        if (!new RegExp(`^/Kad/PdfDocument/${cardId}/${documentId}/[^/]+\\.pdf$`, 'i').test(pdf.pathname) || pdf.search || pdf.hash) throw new Error('invalid_pdf');
        if (typeof item.label !== 'string' || !item.label.trim() || item.label.length > 2000) throw new Error('invalid_label');
        return {url: pdf.href, label: item.label};
      });
      if (new Set(candidates.map(item => item.url)).size !== candidates.length) throw new Error('duplicate_candidates');
      job = {...job, phase: 'documents', candidates};
    } else if (message.type === 'select') {
      if (job.phase !== 'documents' || P.cardURL(url.href) !== job.cardUrl || !job.candidates.some(item => item.url === message.url)) throw new Error('invalid_selection');
      if (message.automatic && (job.candidates.length !== 1 || job.selectedUrl)) throw new Error('invalid_auto_selection');
      job = {...job, selectedUrl: message.url, selectionReason: message.automatic ? 'only_visible_document' : 'employee_selected_visible_document'};
    } else throw new Error('invalid_message');
  }
  await extensionApi.storage.session.set({[key]: job});
  return {job};
}

extensionApi.runtime.onMessage.addListener((message, sender, reply) => {
  const id = sender.tab?.id;
  const previous = locks.get(id) || Promise.resolve();
  const pending = previous.catch(() => {}).then(() => handle(message, sender));
  locks.set(id, pending);
  pending.then(reply, () => reply({error: 'Помощник остановлен: состояние вкладки изменилось. Запустите новый поиск из сервиса.'})).finally(() => {
    if (locks.get(id) === pending) locks.delete(id);
  });
  return true;
});
extensionApi.tabs.onRemoved.addListener(tabId => extensionApi.storage.session.remove(keyFor(tabId)));
