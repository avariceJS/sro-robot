/* Service bridge. PDF bytes live in memory until the service acknowledges receipt. */
(() => {
  const SERVICE_ORIGINS = Object.freeze(/* SRO_SERVICE_ORIGINS */['http://localhost:8000', 'http://127.0.0.1:8000']);
  const api = globalThis.browser || globalThis.chrome;
  function nativePdfPermissions() {
    if (typeof api.runtime?.getBrowserInfo === 'function') return ['webRequest', 'webRequestBlocking', 'webRequestFilterResponse'];
    const agent = globalThis.navigator?.userAgent || '';
    if (/Chrome\/|Edg\//.test(agent) && !/Firefox\//.test(agent)) return ['debugger'];
    return null;
  }
  const P = globalThis.SroKadProtocol;
  const buffers = new Map();
  const controllers = new Map();
  const acquisitions = new Map();
  const MAX = 25 * 1024 * 1024;
  const serviceKey = id => `service:${id}`;
  const kadKey = id => `kad:${id}`;
  function serviceURL(raw) {
    const url = new URL(raw);
    if (!SERVICE_ORIGINS.includes(url.origin) || url.username || url.password || url.pathname !== '/') throw new Error('invalid_service');
    return url.origin;
  }
  async function get(key) { return (await api.storage.session.get(key))[key]; }
  async function cleanup(serviceTab, binding, keepTab = false) {
    controllers.get(binding.tabId)?.abort();
    await globalThis.SroKadWithLock(binding.tabId, async () => {
      buffers.delete(binding.tabId);
      controllers.delete(binding.tabId);
      acquisitions.delete(binding.tabId);
      await api.storage.session.remove([serviceKey(serviceTab), kadKey(binding.tabId)]);
    });
    if (!keepTab) await api.tabs.remove(binding.tabId).catch(() => {});
  }
  async function receive(message, sender) {
    const origin = serviceURL(sender.url);
    const key = serviceKey(sender.tab.id);
    let binding = await get(key);
    if (message.type === 'bridge-resume') return binding ? {requestId: binding.requestId, importStarted: !!binding.importStarted} : {requestId: null};
    if (message.type === 'bridge-start') {
      if (binding) throw new Error('service_busy');
      const pdfPermissions = nativePdfPermissions();
      if (!pdfPermissions) throw new Error('native_pdf_unavailable');
      if (!await api.permissions.contains({permissions: pdfPermissions})) throw new Error('native_pdf_permission');
      const requestId = P.requestId(message.requestId);
      const caseNumber = P.normalizeCase(message.caseNumber);
      const actDate = P.actDate(message.actDate);
      const tab = await api.tabs.create({url: 'about:blank', active: false});
      binding = {requestId, tabId: tab.id, origin};
      const job = {requestId, caseNumber, actDate, phase: 'prepared', startedAt: Date.now(),
                   bridge: {serviceTab: sender.tab.id, origin}};
      await api.storage.session.set({[key]: binding, [kadKey(tab.id)]: job});
      await api.tabs.update(tab.id, {url: P.launchURL(caseNumber, requestId, actDate)});
      return {phase: 'searching'};
    }
    if (!binding || binding.origin !== origin || binding.requestId !== message.requestId) throw new Error('invalid_binding');
    const job = await get(kadKey(binding.tabId));
    if (message.type === 'bridge-importing') {
      if (binding.terminalReason) throw new Error('run_stopped');
      if (controllers.get(binding.tabId)?.signal.aborted) throw new Error('aborted');
      if (job?.phase !== 'ready') throw new Error('pdf_not_ready');
      try { await api.tabs.get(binding.tabId); }
      catch {
        binding = {...binding, terminalReason: 'Вкладка КАД закрыта. Передача документа не выполнена; запустите новый поиск.'};
        await api.storage.session.set({[key]: binding});
        throw new Error('run_stopped');
      }
      // Synchronous admission point: cancellation is no longer promised after this.
      const active = acquisitions.get(binding.tabId);
      if (active) active.importStarted = true;
      await api.storage.session.set({[key]: {...binding, importStarted: true}});
      return {ok: true};
    }
    if (message.type === 'bridge-finish') {
      // The service has an acknowledged task, or explicitly stopped after error.
      await cleanup(sender.tab.id, binding, message.keepTab === true);
      return {ok: true};
    }
    if (message.type !== 'bridge-status') throw new Error('invalid_bridge_message');
    if (binding.terminalReason) return {phase: 'stopped', reason: binding.terminalReason};
    if (!job || Date.now() - job.startedAt > 5 * 60 * 1000) return {phase: 'stopped', reason: 'Запуск прерван или истёк. Повтор автоматически не выполнялся.'};
    if (job.phase === 'ready') {
      if (controllers.get(binding.tabId)?.signal.aborted) return {phase: 'stopped', reason: 'Передача PDF отменена сотрудником.'};
      const body = buffers.get(binding.tabId);
      if (!body) return {phase: 'stopped', reason: 'Браузер перезапустился до передачи документа. Запустите новый поиск.'};
      return {phase: 'ready', body, source: {requestId: job.requestId, caseNumber: job.caseNumber,
        cardUrl: job.cardUrl, candidates: job.candidates, selectedUrl: job.selectedUrl,
        selectionReason: job.selectionReason, ...(job.actDate ? {actDate: job.actDate} : {})}};
    }
    const status = {phase: job.phase, reason: job.reason};
    if (job.gatewayResume && (job.phase === 'searching' || job.phase === 'card'))
      status.notice = 'КАД временно недоступен. Обновляю страницу…';
    return status;
  }
  async function acquire(job, tabId) {
    const selection = P.automaticDocument(job.candidates || [], job.actDate);
    if (!job.bridge || job.phase !== 'documents' || selection.error ||
        job.selectedUrl !== selection.candidate.url || job.selectionReason !== selection.reason) throw new Error('invalid_acquisition');
    const controller = new AbortController();
    controllers.set(tabId, controller);
    acquisitions.set(tabId, {requestId: job.requestId, cardUrl: job.cardUrl});
    const owner = await api.tabs.get(job.bridge.serviceTab);
    if (controller.signal.aborted || serviceURL(owner.url) !== job.bridge.origin) throw new Error('service_left');
    const bytes = await globalThis.SroKadNativePdf.capture({tabId, url: job.selectedUrl, signal: controller.signal});
    if (bytes.length > MAX) throw new Error('pdf_too_large');
    if (new TextDecoder().decode(bytes.slice(0, 5)) !== '%PDF-') throw new Error('not_pdf');
    let binary = '';
    for (let at = 0; at < bytes.length; at += 32768) binary += String.fromCharCode(...bytes.subarray(at, at + 32768));
    const binding = await get(serviceKey(job.bridge.serviceTab));
    if (controller.signal.aborted || !binding || binding.tabId !== tabId || binding.requestId !== job.requestId) throw new Error('acquisition_cancelled');
    buffers.set(tabId, btoa(binary));
    // Keep cancellation identity through ready/receipt, including the async state write.
    return {...job, phase: 'ready'};
  }
  api.tabs.onRemoved.addListener(async id => {
    controllers.get(id)?.abort();
    buffers.delete(id);
    controllers.delete(id);
    acquisitions.delete(id);
    const all = await api.storage.session.get(null);
    const owner = Object.entries(all).find(([storedKey, value]) =>
      storedKey.startsWith('service:') && value?.tabId === id);
    if (owner) {
      const [storedKey, binding] = owner;
      const serviceTab = Number(storedKey.slice('service:'.length));
      await globalThis.SroKadWithLock(serviceTab, async () => {
        const current = await get(storedKey);
        if (current?.tabId !== id || current.requestId !== binding.requestId) return;
        await api.storage.session.set({[storedKey]: {
          ...current,
          terminalReason: 'Вкладка КАД закрыта. Передача документа не выполнена; запустите новый поиск.',
        }});
        await api.storage.session.remove(kadKey(id));
      });
      return;
    }
    const binding = await get(serviceKey(id));
    if (binding) {
      await cleanup(id, binding);
    }
  });
  function cancel(tabId, requestId, url) {
    const active = acquisitions.get(tabId);
    try {
      if (active && !active.importStarted && active.requestId === requestId && P.cardURL(url) === active.cardUrl)
        controllers.get(tabId)?.abort();
    } catch {} // An unrelated page/run cannot cancel the owned capture.
  }
  function importStarted(tabId, requestId) {
    const active = acquisitions.get(tabId);
    return !!active && active.requestId === requestId && !!active.importStarted;
  }
  globalThis.SroKadBridge = {receive, acquire, cancel, importStarted};
})();
