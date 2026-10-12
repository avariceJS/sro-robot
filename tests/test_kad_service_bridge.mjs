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

const PDF_BYTES = new TextEncoder().encode('%PDF-1.7 synthetic pdf bytes');

async function harness({
  captureImpl = async () => new Uint8Array(PDF_BYTES),
  permissionsGranted = true,
  browserAvailable = true,
} = {}) {
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
    runtime: {
      id: 'synthetic-extension-id',
      ...(browserAvailable ? {getBrowserInfo: async () => ({name: 'Synthetic Firefox'})} : {}),
    },
    permissions: {
      calls: [],
      async contains(value) { this.calls.push(value); return permissionsGranted; },
    },
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
    setTimeout, clearTimeout, btoa,
    SroKadWithLock: withLock,
    SroKadNativePdf: {capture: captureImpl},
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

test('bridge start requires native browser support and the complete capture permission set', async t => {
  await t.test('missing Firefox browser API', async () => {
    const h = await harness({browserAvailable: false});
    await assert.rejects(
      h.bridge.receive({type: 'bridge-start', requestId: RUN, caseNumber: CASE}, serviceSender()),
      /native_pdf_unavailable/,
    );
    assert.equal(h.extensionApi.permissions.calls.length, 0);
    assert.equal(h.tabs.size, 0);
    assert.equal(h.stored.size, 0);
  });
  await t.test('permission denied', async () => {
    const h = await harness({permissionsGranted: false});
    await assert.rejects(
      h.bridge.receive({type: 'bridge-start', requestId: RUN, caseNumber: CASE}, serviceSender()),
      /native_pdf_permission/,
    );
    assert.equal(JSON.stringify(h.extensionApi.permissions.calls), JSON.stringify([
      {permissions: ['webRequest', 'webRequestBlocking', 'webRequestFilterResponse']},
    ]));
    assert.equal(h.tabs.size, 0);
    assert.equal(h.stored.size, 0);
  });
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

test('native capture receives the bound tab and selected URL; validated bytes and metadata reach ready', async () => {
  let captureArgs;
  const h = await harness({captureImpl: async args => { captureArgs = args; return new Uint8Array(PDF_BYTES); }});
  const binding = await started(h);
  const candidateInventory = [
    {url: PDF_URL, label: 'Synthetic selected act', actDate: '2024-02-29'},
    {url: PDF_URL.replace('decision.pdf', 'other.pdf'), label: 'Synthetic other event', actDate: '2024-03-01'},
  ];
  const job = {...eligibleJob({...binding, serviceTab: 1}), actDate: '2024-02-29',
    candidates: candidateInventory, selectionReason: 'unique_act_date'};
  h.stored.set(`kad:${binding.tabId}`, job);
  const ready = await h.bridge.acquire(job, binding.tabId);
  assert.equal(ready.phase, 'ready');
  assert.equal(captureArgs.tabId, binding.tabId);
  assert.equal(captureArgs.url, PDF_URL);
  assert.equal(captureArgs.signal instanceof AbortSignal, true);
  h.stored.set(`kad:${binding.tabId}`, ready);
  const status = await h.bridge.receive({type: 'bridge-status', requestId: RUN}, serviceSender());
  assert.equal(status.phase, 'ready');
  assert.equal(status.source.requestId, RUN);
  assert.equal(status.source.caseNumber, CASE);
  assert.equal(status.source.cardUrl, CARD);
  assert.equal(status.source.selectedUrl, PDF_URL);
  assert.equal(status.source.actDate, '2024-02-29');
  assert.equal(status.source.selectionReason, 'unique_act_date');
  assert.deepEqual(status.source.candidates, candidateInventory);
  assert.equal(Buffer.from(status.body, 'base64').subarray(0, 5).toString(), '%PDF-');
});

test('bridge validates native capture bytes for size and PDF magic', async t => {
  const hugeBytes = new Uint8Array(25 * 1024 * 1024 + 1);
  hugeBytes.set(new TextEncoder().encode('%PDF-'));
  const cases = [
    ['oversized capture', () => hugeBytes, /pdf_too_large/],
    ['non-PDF magic', () => new TextEncoder().encode('synthetic non-pdf bytes'), /not_pdf/],
  ];
  for (const [name, makeBytes, error] of cases) {
    await t.test(name, async () => {
      const h = await harness({captureImpl: async () => makeBytes()});
      const binding = await started(h);
      const job = eligibleJob({...binding, serviceTab: 1});
      h.stored.set(`kad:${binding.tabId}`, job);
      await assert.rejects(h.bridge.acquire(job, binding.tabId), error);
      assert.equal(h.stored.get(`kad:${binding.tabId}`).phase, 'documents', 'failed acquisition does not persist ready state');
    });
  }
});

test('acquisition requires service tab to remain on its bound origin', async () => {
  let captureCalled = false;
  const h = await harness({captureImpl: async () => { captureCalled = true; return new Uint8Array(PDF_BYTES); }});
  const binding = await started(h);
  const job = eligibleJob({...binding, serviceTab: 1});
  await h.extensionApi.tabs.update(1, {url: 'http://127.0.0.1:8000/'});
  await assert.rejects(h.bridge.acquire(job, binding.tabId), /service_left/);
  assert.equal(captureCalled, false);
});

test('finish clears only its service binding, buffered body, and own KAD tab', async () => {
  const h = await harness({captureImpl: async () => new Uint8Array(PDF_BYTES)});
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
  const h = await harness({captureImpl: async () => new Uint8Array(PDF_BYTES)});
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
  test(`in-flight native capture publishes no ready bytes after ${finishMode}`, async () => {
    let releaseCapture;
    let startedCapture;
    const captureStarted = new Promise(resolve => { startedCapture = resolve; });
    const pendingCapture = new Promise(resolve => { releaseCapture = resolve; });
    const h = await harness({captureImpl: ({signal}) => {
      startedCapture(signal);
      return pendingCapture;
    }});
    const binding = await started(h);
    const job = eligibleJob({...binding, serviceTab: 1});
    h.stored.set(`kad:${binding.tabId}`, job);
    const acquiring = h.withLock(binding.tabId, () => h.bridge.acquire(job, binding.tabId));
    const signal = await captureStarted;

    let cleanup;
    if (finishMode === 'bridge-finish') {
      cleanup = h.bridge.receive({type: 'bridge-finish', requestId: RUN}, serviceSender());
    } else {
      cleanup = h.extensionApi.tabs.remove(1);
    }
    if (!signal.aborted) await new Promise(resolve => signal.addEventListener('abort', resolve, {once: true}));
    assert.equal(signal.aborted, true, 'cleanup aborts the outstanding native capture before waiting for the KAD lock');
    releaseCapture(new Uint8Array(PDF_BYTES));
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


test('failed run cleanup can retain the card while removing its binding and bytes', async () => {
  const h = await harness();
  const binding = await started(h);
  await h.bridge.receive({type: 'bridge-finish', requestId: RUN, keepTab: true}, serviceSender());
  assert.ok(h.tabs.has(binding.tabId));
  assert.equal(h.stored.has('service:1'), false);
  assert.equal(h.stored.has(`kad:${binding.tabId}`), false);
});

test('acquisition rejects when its service binding changes while native capture is in flight', async () => {
  let releaseCapture;
  let startedCapture;
  const captureStarted = new Promise(resolve => { startedCapture = resolve; });
  const pendingCapture = new Promise(resolve => { releaseCapture = resolve; });
  const h = await harness({captureImpl: ({signal}) => {
    startedCapture(signal);
    return pendingCapture;
  }});
  const binding = await started(h);
  const job = eligibleJob({...binding, serviceTab: 1});
  h.stored.set(`kad:${binding.tabId}`, job);
  const acquiring = h.bridge.acquire(job, binding.tabId);
  const signal = await captureStarted;
  h.stored.set('service:1', {...binding, requestId: OTHER_RUN});
  releaseCapture(new Uint8Array(PDF_BYTES));
  await assert.rejects(acquiring, /acquisition_cancelled/);
  assert.equal(signal.aborted, false);
  assert.equal(h.stored.get(`kad:${binding.tabId}`).phase, 'documents');
  await assert.rejects(h.bridge.receive({type: 'bridge-status', requestId: RUN}, serviceSender()), /invalid_binding/);
});
