/* Local service bridge. PDF bytes live in memory until the service acknowledges receipt. */
(() => {
  const api = globalThis.browser || globalThis.chrome;
  const P = globalThis.SroKadProtocol;
  const buffers = new Map();
  const controllers = new Map();
  const MAX = 25 * 1024 * 1024;
  const serviceKey = id => `service:${id}`;
  const kadKey = id => `kad:${id}`;
  function serviceURL(raw) {
    const url = new URL(raw);
    if (!['http://localhost:8000', 'http://127.0.0.1:8000'].includes(url.origin) || url.username || url.password || url.pathname !== '/') throw new Error('invalid_service');
    return url.origin;
  }
  async function get(key) { return (await api.storage.session.get(key))[key]; }
  async function cleanup(serviceTab, binding) {
    controllers.get(binding.tabId)?.abort();
    await globalThis.SroKadWithLock(binding.tabId, async () => {
      buffers.delete(binding.tabId);
      await api.storage.session.remove([serviceKey(serviceTab), kadKey(binding.tabId)]);
    });
    await api.tabs.remove(binding.tabId).catch(() => {});
  }
  async function receive(message, sender) {
    const origin = serviceURL(sender.url);
    const key = serviceKey(sender.tab.id);
    let binding = await get(key);
    if (message.type === 'bridge-resume') return binding ? {requestId: binding.requestId, importStarted: !!binding.importStarted} : {requestId: null};
    if (message.type === 'bridge-start') {
      if (binding) throw new Error('service_busy');
      const requestId = P.requestId(message.requestId);
      const caseNumber = P.normalizeCase(message.caseNumber);
      const tab = await api.tabs.create({url: 'about:blank', active: false});
      binding = {requestId, tabId: tab.id, origin};
      const job = {requestId, caseNumber, phase: 'prepared', startedAt: Date.now(),
                   bridge: {serviceTab: sender.tab.id, origin}};
      await api.storage.session.set({[key]: binding, [kadKey(tab.id)]: job});
      await api.tabs.update(tab.id, {url: P.launchURL(caseNumber, requestId)});
      return {phase: 'searching'};
    }
    if (!binding || binding.origin !== origin || binding.requestId !== message.requestId) throw new Error('invalid_binding');
    const job = await get(kadKey(binding.tabId));
    if (message.type === 'bridge-importing') {
      if (job?.phase !== 'ready') throw new Error('pdf_not_ready');
      await api.storage.session.set({[key]: {...binding, importStarted: true}});
      return {ok: true};
    }
    if (message.type === 'bridge-finish') {
      // The service has an acknowledged task, or explicitly stopped after error.
      await cleanup(sender.tab.id, binding);
      return {ok: true};
    }
    if (message.type !== 'bridge-status') throw new Error('invalid_bridge_message');
    if (!job || Date.now() - job.startedAt > 5 * 60 * 1000) return {phase: 'stopped', reason: 'Запуск прерван или истёк. Повтор автоматически не выполнялся.'};
    if (job.phase === 'ready') {
      const body = buffers.get(binding.tabId);
      if (!body) return {phase: 'stopped', reason: 'Браузер перезапустился до передачи документа. Запустите новый поиск.'};
      return {phase: 'ready', body, source: {requestId: job.requestId, caseNumber: job.caseNumber,
        cardUrl: job.cardUrl, candidates: job.candidates, selectedUrl: job.selectedUrl,
        selectionReason: job.selectionReason}};
    }
    return {phase: job.phase, reason: job.reason};
  }
  async function acquire(job, tabId) {
    if (!job.bridge || job.phase !== 'documents' || job.candidates.length !== 1 ||
        job.selectedUrl !== job.candidates[0].url || job.selectionReason !== 'only_visible_document') throw new Error('invalid_acquisition');
    const controller = new AbortController();
    controllers.set(tabId, controller);
    const timer = setTimeout(() => controller.abort(), 60000);
    try {
      const owner = await api.tabs.get(job.bridge.serviceTab);
      if (controller.signal.aborted || serviceURL(owner.url) !== job.bridge.origin) throw new Error('service_left');
      const response = await fetch(job.selectedUrl, {credentials: 'include', cache: 'no-store', signal: controller.signal});
      const final = P.kadURL(response.url);
      const original = new URL(job.selectedUrl);
      const expectedPath = original.pathname.replace('/Kad/PdfDocument/', '/Document/Pdf/');
      if (!response.ok || ![original.pathname, expectedPath].includes(final.pathname) ||
          (final.search && final.search !== '?isAddStamp=True') || final.hash ||
          !(response.headers.get('content-type') || '').toLowerCase().startsWith('application/pdf')) throw new Error('not_pdf');
      if (Number(response.headers.get('content-length') || 0) > MAX) throw new Error('pdf_too_large');
      const reader = response.body.getReader();
      const chunks = []; let size = 0;
      while (true) {
        const {value, done} = await reader.read();
        if (done) break;
        size += value.length;
        if (size > MAX) { await reader.cancel(); throw new Error('pdf_too_large'); }
        chunks.push(value);
      }
      const bytes = new Uint8Array(size); let offset = 0;
      for (const part of chunks) { bytes.set(part, offset); offset += part.length; }
      if (new TextDecoder().decode(bytes.slice(0, 5)) !== '%PDF-') throw new Error('not_pdf');
      let binary = '';
      for (let at = 0; at < bytes.length; at += 32768) binary += String.fromCharCode(...bytes.subarray(at, at + 32768));
      const binding = await get(serviceKey(job.bridge.serviceTab));
      if (controller.signal.aborted || !binding || binding.tabId !== tabId || binding.requestId !== job.requestId) throw new Error('acquisition_cancelled');
      buffers.set(tabId, btoa(binary));
      return {...job, phase: 'ready'};
    } finally { clearTimeout(timer); controllers.delete(tabId); }
  }
  api.tabs.onRemoved.addListener(async id => {
    controllers.get(id)?.abort();
    buffers.delete(id);
    const binding = await get(serviceKey(id));
    if (binding) {
      await cleanup(id, binding);
    }
  });
  globalThis.SroKadBridge = {receive, acquire};
})();
