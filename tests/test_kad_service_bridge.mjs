import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';

const CASE = 'А99-12345/2099';
const RUN = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee';
const OTHER_RUN = 'bbbbbbbb-cccc-dddd-eeee-ffffffffffff';
const CARD_ID = '11111111-2222-3333-4444-555555555555';
const DOC_ID = '12345678-1234-1234-1234-123456789012';
const PDF_URL = `https://kad.arbitr.ru/Kad/PdfDocument/${CARD_ID}/${DOC_ID}/decision.pdf`;
const CARD = `https://kad.arbitr.ru/Card/${CARD_ID}`;
const ROOT = new URL('../browser-extension/kad-helper/', import.meta.url);

async function harness({fetchImpl = async () => { throw new Error('unexpected_fetch'); }} = {}) {
  const stored = new Map();
  const tabs = new Map();
  const removedListeners = [];
  const locks = new Map();
  const withLock = (id, action) => {
    const pending = (locks.get(id) || Promise.resolve()).catch(() => {}).then(action);
    locks.set(id, pending);
    pending.finally(() => { if (locks.get(id) === pending) locks.delete(id); }).catch(() => {});
    return pending;
  };
  let nextTab = 100;
  const extensionApi = {
    runtime: { id: 'synthetic-extension-id' },
    storage: { session: {
      async get(key) { return { [key]: stored.get(key) }; },
      async set(values) { for (const [key, value] of Object.entries(values)) stored.set(key, value); },
      async remove(keys) { for (const key of Array.isArray(keys) ? keys : [keys]) stored.delete(key); },
    } },
    tabs: {
      onRemoved: { addListener(fn) { removedListeners.push(fn); } },
      async create(options) {
        const tab = {id: nextTab++, ...options};
        tabs.set(tab.id, tab);
        return {...tab};
      },
      async update(id, values) {
        const tab = tabs.get(id);
        if (!tab) throw new Error('missing_tab');
        Object.assign(tab, values);
        return {...tab};
      },
      async get(id) {
        const tab = tabs.get(id);
        if (!tab) throw new Error('missing_tab');
        return {...tab};
      },
      async remove(id) {
        if (!tabs.has(id)) throw new Error('missing_tab');
        tabs.delete(id);
        for (const listener of removedListeners) await listener(id);
      },
    },
  };
  const sandbox = {
    URL, URLSearchParams, Date, Map, Promise, Error, Number, Array, String,
    RegExp, Object, Uint8Array, TextDecoder, AbortController,
    setTimeout, clearTimeout, btoa, fetch: fetchImpl,
    SroKadWithLock: withLock,
  };
  sandbox.chrome = extensionApi;
  const context = vm.createContext(sandbox);
  for (const file of ['protocol.js', 'bridge.js']) {
    const source = await readFile(new URL(file, ROOT), 'utf8');
    vm.runInContext(source, context, {filename: file});
  }
  return {bridge: context.SroKadBridge, extensionApi, stored, tabs, removedListeners, withLock};
}

const serviceSender = (tabId = 1, url = 'http://localhost:8000/') => ({
  tab: {id: tabId}, url,
});

function pdfResponse({
  url = PDF_URL,
  ok = true,
  status = 200,
  contentType = 'application/pdf',
  contentLength,
  chunks = [new TextEncoder().encode('%PDF-1.7 synthetic pdf bytes')],
} = {}) {
  const values = new Map([['content-type', contentType]]);
  if (contentLength !== undefined) values.set('content-length', String(contentLength));
  let index = 0;
  return {
    url, ok, status,
    headers: {get(name) { return values.get(name.toLowerCase()) ?? null; }},
    body: {getReader() {
      return {
        async read() {
          if (index >= chunks.length) return {done: true};
          return {done: false, value: chunks[index++]};
        },
        async cancel() {},
      };
    }},
  };
}

async function started(h, {serviceTab = 1, serviceUrl = 'http://localhost:8000/'} = {}) {
  h.tabs.set(serviceTab, {id: serviceTab, url: serviceUrl});
  const result = await h.bridge.receive({type: 'bridge-start', requestId: RUN, caseNumber: CASE}, serviceSender(serviceTab, serviceUrl));
  assert.equal(result.phase, 'searching');
  const binding = h.stored.get(`service:${serviceTab}`);
  assert.ok(binding);
  return binding;
}

function eligibleJob(binding) {
  return {
    requestId: RUN,
    caseNumber: CASE,
    phase: 'documents',
    startedAt: Date.now(),
    bridge: {serviceTab: binding.serviceTab, origin: binding.origin},
    candidates: [{url: PDF_URL, label: 'Synthetic.pdf'}],
    selectedUrl: PDF_URL,
    selectionReason: 'only_visible_document',
    cardUrl: CARD,
  };
}

test('service URL accepts only exact localhost origins, port 8000, and root path', async () => {
  const h = await harness();
  for (const url of [
    'http://localhost:8000/',
    'http://127.0.0.1:8000/',
  ]) {
    const result = await h.bridge.receive({type: 'bridge-start', requestId: RUN, caseNumber: CASE}, serviceSender(1, url));
    assert.equal(result.phase, 'searching');
    const binding = h.stored.get('service:1');
    assert.equal(binding.origin, new URL(url).origin);
    await h.bridge.receive({type: 'bridge-finish', requestId: RUN}, serviceSender(1, url));
  }
  for (const url of [
    'http://localhost/',
    'http://localhost:8001/',
    'http://127.0.0.1:8000/other',
    'http://localhost:8000/kad-helper',
    'https://localhost:8000/',
    'http://localhost.evil.test:8000/',
    'http://user@localhost:8000/',
  ]) {
    await assert.rejects(
      h.bridge.receive({type: 'bridge-start', requestId: RUN, caseNumber: CASE}, serviceSender(9, url)),
      /invalid_service/,
      url,
    );
  }
});

test('start creates an inactive KAD tab and binds isolated state to the originating service tab', async () => {
  const h = await harness();
  const first = await started(h, {serviceTab: 1});
  const second = await started(h, {serviceTab: 2, serviceUrl: 'http://127.0.0.1:8000/'});
  assert.notEqual(first.tabId, second.tabId);
  assert.equal(h.tabs.get(first.tabId).active, false);
  assert.equal(h.tabs.get(second.tabId).active, false);
  assert.equal(h.tabs.get(first.tabId).url, `https://kad.arbitr.ru/#sro_robot=1&case=${encodeURIComponent(CASE)}&run=${RUN}`);
  assert.equal(h.stored.get(`kad:${first.tabId}`).bridge.serviceTab, 1);
  assert.equal(h.stored.get(`kad:${second.tabId}`).bridge.serviceTab, 2);
  assert.equal((await h.bridge.receive({type: 'bridge-status', requestId: RUN}, serviceSender(1))).phase, 'prepared');
  assert.equal((await h.bridge.receive({type: 'bridge-status', requestId: RUN}, serviceSender(2, 'http://127.0.0.1:8000/'))).phase, 'prepared');
  await assert.rejects(
    h.bridge.receive({type: 'bridge-status', requestId: RUN}, serviceSender(1, 'http://127.0.0.1:8000/')),
    /invalid_binding/,
  );
  await assert.rejects(
    h.bridge.receive({type: 'bridge-start', requestId: OTHER_RUN, caseNumber: CASE}, serviceSender(1)),
    /service_busy/,
  );
});

test('status reports the current KAD phase and returns only the ready job source and buffered bytes', async () => {
  const h = await harness();
  const binding = await started(h);
  const jobKey = `kad:${binding.tabId}`;
  const job = {...eligibleJob({...binding, serviceTab: 1}), phase: 'card', reason: undefined};
  h.stored.set(jobKey, job);
  assert.equal((await h.bridge.receive({type: 'bridge-status', requestId: RUN}, serviceSender())).phase, 'card');
  job.phase = 'stopped';
  job.reason = 'synthetic stop';
  h.stored.set(jobKey, job);
  const stopped = await h.bridge.receive({type: 'bridge-status', requestId: RUN}, serviceSender());
  assert.equal(stopped.phase, 'stopped');
  assert.equal(stopped.reason, 'synthetic stop');

  h.stored.set(jobKey, {...job, phase: 'ready'});
  assert.equal((await h.bridge.receive({type: 'bridge-status', requestId: RUN}, serviceSender())).phase, 'stopped', 'ready without in-memory bytes stops');
});

test('only a selected sole PDF with matching redirect, MIME, size, and magic reaches ready', async () => {
  const matchingRedirect = `https://kad.arbitr.ru/Document/Pdf/${CARD_ID}/${DOC_ID}/decision.pdf?isAddStamp=True`;
  const h = await harness({fetchImpl: async () => pdfResponse({url: matchingRedirect})});
  const binding = await started(h);
  h.stored.set(`kad:${binding.tabId}`, eligibleJob({...binding, serviceTab: 1}));
  const ready = await h.bridge.acquire(h.stored.get(`kad:${binding.tabId}`), binding.tabId);
  assert.equal(ready.phase, 'ready');
  h.stored.set(`kad:${binding.tabId}`, ready);
  const status = await h.bridge.receive({type: 'bridge-status', requestId: RUN}, serviceSender());
  assert.equal(status.phase, 'ready');
  assert.equal(status.source.requestId, RUN);
  assert.equal(status.source.caseNumber, CASE);
  assert.equal(status.source.cardUrl, CARD);
  assert.equal(status.source.selectedUrl, PDF_URL);
  assert.equal(Buffer.from(status.body, 'base64').subarray(0, 5).toString(), '%PDF-');
});

test('acquisition rejects failed fetches, off-host or different-card redirects, HTML, bad magic, and oversized bodies', async t => {
  const hugeChunk = new Uint8Array(25 * 1024 * 1024 + 1);
  hugeChunk.set(new TextEncoder().encode('%PDF-'));
  const cases = [
    ['failed fetch', () => pdfResponse({ok: false, status: 503}), /not_pdf/],
    ['off-host redirect', () => pdfResponse({url: PDF_URL.replace('kad.arbitr.ru', 'evil.test')}), /invalid_url|not_pdf/],
    ['different card redirect', () => pdfResponse({url: PDF_URL.replace(CARD_ID, '66666666-7777-8888-9999-000000000000')}), /not_pdf/],
    ['HTML MIME', () => pdfResponse({contentType: 'text/html'}), /not_pdf/],
    ['oversized declared response', () => pdfResponse({contentLength: 25 * 1024 * 1024 + 1}), /pdf_too_large/],
    ['oversized streamed response', () => pdfResponse({chunks: [hugeChunk]}), /pdf_too_large/],
    ['non-PDF magic', () => pdfResponse({chunks: [new TextEncoder().encode('hello synthetic content')]}), /not_pdf/],
  ];
  for (const [name, makeResponse, error] of cases) {
    await t.test(name, async () => {
      const h = await harness({fetchImpl: async () => makeResponse()});
      const binding = await started(h);
      const job = eligibleJob({...binding, serviceTab: 1});
      h.stored.set(`kad:${binding.tabId}`, job);
      await assert.rejects(h.bridge.acquire(job, binding.tabId), error);
      assert.equal(h.stored.get(`kad:${binding.tabId}`).phase, 'documents', 'failed acquisition does not persist ready state');
    });
  }
});

test('acquisition requires service tab to remain on its bound origin', async () => {
  const h = await harness();
  const binding = await started(h);
  const job = eligibleJob({...binding, serviceTab: 1});
  await h.extensionApi.tabs.update(1, {url: 'http://127.0.0.1:8000/'});
  await assert.rejects(h.bridge.acquire(job, binding.tabId), /service_left/);
});

test('finish clears only its service binding, buffered body, and own KAD tab', async () => {
  const h = await harness({fetchImpl: async () => pdfResponse()});
  const binding = await started(h);
  const second = await started(h, {serviceTab: 2, serviceUrl: 'http://127.0.0.1:8000/'});
  h.stored.set(`kad:${binding.tabId}`, eligibleJob({...binding, serviceTab: 1}));
  const ready = await h.bridge.acquire(h.stored.get(`kad:${binding.tabId}`), binding.tabId);
  h.stored.set(`kad:${binding.tabId}`, ready);
  assert.equal((await h.bridge.receive({type: 'bridge-status', requestId: RUN}, serviceSender())).phase, 'ready');

  assert.equal((await h.bridge.receive({type: 'bridge-finish', requestId: RUN}, serviceSender())).ok, true);
  assert.equal(h.stored.has('service:1'), false);
  assert.equal(h.stored.has(`kad:${binding.tabId}`), false);
  assert.equal(h.tabs.has(binding.tabId), false);
  assert.equal(h.stored.has('service:2'), true);
  assert.equal(h.tabs.has(second.tabId), true);
  await assert.rejects(h.bridge.receive({type: 'bridge-status', requestId: RUN}, serviceSender()), /invalid_binding/);
});

test('service origin change invalidates status binding and an owner tab close removes its paired KAD tab', async () => {
  const h = await harness();
  const binding = await started(h);
  await assert.rejects(
    h.bridge.receive({type: 'bridge-status', requestId: RUN}, serviceSender(1, 'http://127.0.0.1:8000/')),
    /invalid_binding/,
  );
  for (const listener of h.removedListeners) await listener(1);
  assert.equal(h.stored.has('service:1'), false);
  assert.equal(h.stored.has(`kad:${binding.tabId}`), false);
  assert.equal(h.tabs.has(binding.tabId), false);
});

test('bridge-resume returns only the binding belonging to the requesting service tab', async () => {
  const h = await harness({fetchImpl: async () => pdfResponse()});
  const first = await started(h, {serviceTab: 1});
  const second = await started(h, {serviceTab: 2, serviceUrl: 'http://127.0.0.1:8000/'});
  await assert.rejects(
    h.bridge.receive({type: 'bridge-importing', requestId: RUN}, serviceSender(1)),
    /pdf_not_ready/,
  );
  const ready = await h.bridge.acquire(eligibleJob({...first, serviceTab: 1}), first.tabId);
  h.stored.set(`kad:${first.tabId}`, ready);
  assert.equal((await h.bridge.receive({type: 'bridge-importing', requestId: RUN}, serviceSender(1))).ok, true);
  const firstResume = await h.bridge.receive({type: 'bridge-resume'}, serviceSender(1));
  assert.equal(firstResume.requestId, RUN);
  assert.equal(firstResume.importStarted, true);
  const secondResume = await h.bridge.receive({type: 'bridge-resume'}, serviceSender(2, 'http://127.0.0.1:8000/'));
  assert.equal(secondResume.requestId, RUN);
  assert.equal(secondResume.importStarted, false);
  assert.equal((await h.bridge.receive({type: 'bridge-resume'}, serviceSender(3))).requestId, null);
  assert.notEqual(first.tabId, second.tabId);
});

for (const finishMode of ['bridge-finish', 'service-tab removal']) {
  test(`in-flight acquisition publishes no ready bytes after ${finishMode}`, async () => {
    let releaseResponse;
    let startedFetch;
    const fetchStarted = new Promise(resolve => { startedFetch = resolve; });
    const pendingResponse = new Promise(resolve => { releaseResponse = resolve; });
    const h = await harness({fetchImpl: (_url, options) => {
      startedFetch(options.signal);
      return pendingResponse;
    }});
    const binding = await started(h);
    const job = eligibleJob({...binding, serviceTab: 1});
    h.stored.set(`kad:${binding.tabId}`, job);
    const acquiring = h.withLock(binding.tabId, () => h.bridge.acquire(job, binding.tabId));
    const signal = await fetchStarted;

    let cleanup;
    if (finishMode === 'bridge-finish') {
      cleanup = h.bridge.receive({type: 'bridge-finish', requestId: RUN}, serviceSender());
    } else {
      cleanup = h.extensionApi.tabs.remove(1);
    }
    if (!signal.aborted) await new Promise(resolve => signal.addEventListener('abort', resolve, {once: true}));
    assert.equal(signal.aborted, true, 'cleanup aborts the outstanding fetch before waiting for the KAD lock');
    releaseResponse(pdfResponse());
    await assert.rejects(acquiring, /acquisition_cancelled/);
    await cleanup;
    assert.equal(h.stored.has('service:1'), false);
    assert.equal(h.stored.has(`kad:${binding.tabId}`), false);
    assert.equal(h.tabs.has(binding.tabId), false);
    if (finishMode === 'bridge-finish') {
      await assert.rejects(h.bridge.receive({type: 'bridge-status', requestId: RUN}, serviceSender()), /invalid_binding/);
    } else {
      assert.equal((await h.bridge.receive({type: 'bridge-resume'}, serviceSender())).requestId, null);
    }
  });
}
