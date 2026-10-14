import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';

const CASE = 'А99-12345/2099';
const RUN = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee';
const CARD_ID = '11111111-2222-3333-4444-555555555555';
const OTHER_CARD_ID = '66666666-7777-8888-9999-000000000000';
const CARD = `https://kad.arbitr.ru/Card/${CARD_ID}`;
const PDF = `https://kad.arbitr.ru/Kad/PdfDocument/${CARD_ID}/12345678-1234-1234-1234-123456789012/decision.pdf`;
const OTHER_PDF = `https://kad.arbitr.ru/Kad/PdfDocument/${CARD_ID}/aaaaaaaa-1234-1234-1234-123456789012/order.pdf`;
const NON_UUID_PDF = `https://kad.arbitr.ru/Kad/PdfDocument/${CARD_ID}/document-id/ruling.pdf`;
const OTHER_CARD_PDF = `https://kad.arbitr.ru/Kad/PdfDocument/${OTHER_CARD_ID}/12345678-1234-1234-1234-123456789012/decision.pdf`;
const ROOT = new URL('../browser-extension/kad-helper/', import.meta.url);

async function harness({browserOnly = false, withImportScripts = true, nativeCapture = null} = {}) {
  const stored = new Map();
  const tabs = new Map();
  let listener;
  const removedListeners = [];
  let nextTabId = 100;
  const protocolSource = await readFile(new URL('protocol.js', ROOT), 'utf8');
  const nativeSource = await readFile(new URL('native-pdf.js', ROOT), 'utf8');
  const bridgeSource = await readFile(new URL('bridge.js', ROOT), 'utf8');
  const extensionApi = {
    runtime: {
      id: 'synthetic-extension-id',
      getBrowserInfo: async () => ({name: 'Firefox'}),
      onMessage: { addListener(fn) { listener = fn; } },
    },
    permissions: {contains: async () => true},
    storage: { session: {
      async get(key) { return { [key]: stored.get(key) }; },
      async set(values) { for (const [key, value] of Object.entries(values)) stored.set(key, value); },
      async remove(keys) { for (const key of Array.isArray(keys) ? keys : [keys]) stored.delete(key); },
    } },
    tabs: {
      onRemoved: { addListener(fn) { removedListeners.push(fn); } },
      async create(options) {
        const tab = {id: nextTabId++, ...options};
        tabs.set(tab.id, tab);
        return {...tab};
      },
      async update(id, values) {
        if (!tabs.has(id)) throw new Error('missing_tab');
        Object.assign(tabs.get(id), values);
        return {...tabs.get(id)};
      },
      async get(id) {
        if (!tabs.has(id)) throw new Error('missing_tab');
        return {...tabs.get(id)};
      },
      async remove(id) {
        if (!tabs.delete(id)) throw new Error('missing_tab');
        for (const fn of removedListeners) await fn(id);
      },
    },
  };
  const sandbox = {
    URL,
    URLSearchParams,
    Uint8Array,
    TextDecoder,
    AbortController,
    btoa,
    setTimeout,
    Date,
    Map,
    Promise,
    Error,
    Number,
    Array,
    String,
    RegExp,
    Object,
  };
  sandbox[browserOnly ? 'browser' : 'chrome'] = extensionApi;
  const context = vm.createContext(sandbox);
  if (withImportScripts) context.importScripts = file => {
    const source = file === 'protocol.js' ? protocolSource : file === 'native-pdf.js' ? nativeSource : file === 'bridge.js' ? bridgeSource : null;
    if (!source) throw new Error('unexpected_import');
    vm.runInContext(source, context, { filename: file });
  };
  else {
    vm.runInContext(protocolSource, context, { filename: 'protocol.js' });
    vm.runInContext(bridgeSource, context, { filename: 'bridge.js' });
  }
  const backgroundSource = await readFile(new URL('background.js', ROOT), 'utf8');
  vm.runInContext(backgroundSource, context, { filename: 'background.js' });
  if (nativeCapture) context.SroKadNativePdf = {capture: nativeCapture};
  assert.equal(typeof listener, 'function', 'background must register a message listener');

  const sender = (tabId = 1, overrides = {}) => ({
    id: extensionApi.runtime.id,
    tab: { id: tabId },
    frameId: 0,
    url: 'https://kad.arbitr.ru/',
    ...overrides,
  });
  const send = (message, from = sender()) => new Promise(resolve => {
    const keepChannelOpen = listener(message, from, resolve);
    assert.equal(keepChannelOpen, true);
  });
  return { context, extensionApi, stored, tabs, removedListeners, send, sender };
}

test('protocol normalizes the synthetic case and rejects malicious case and URL inputs', async () => {
  const protocolSource = await readFile(new URL('protocol.js', ROOT), 'utf8');
  const context = vm.createContext({ URL, URLSearchParams });
  vm.runInContext(protocolSource, context);
  const p = context.SroKadProtocol;
  assert.equal(p.normalizeCase(` ${CASE.toLowerCase()} `), CASE);
  assert.equal(p.cardURL(CARD), CARD);
  assert.equal(p.launchURL(CASE, RUN), `https://kad.arbitr.ru/#sro_robot=1&case=${encodeURIComponent(CASE)}&run=${RUN}`);

  for (const value of [
    'A99-12345/2099/extra', 'A99-12345/209', 'A99-12345/2099?x=1',
    'A99-12345/20\n99', 'X A99-12345/2099', 99, null,
  ]) assert.throws(() => p.normalizeCase(value), /invalid_case/);
  for (const value of [
    CARD.replace('https://', 'http://'),
    CARD.replace('kad.arbitr.ru', 'kad.arbitr.ru.evil.test'),
    CARD.replace('https://', 'https://user@'),
    `${CARD}?redirect=https://evil.test`, `${CARD}#fragment`, `${CARD}/extra`,
    'https://kad.arbitr.ru\\@evil.test/Card/' + CARD_ID,
    'https://kad.arbitr.ru/Kad/PdfDocument/' + CARD_ID + '/x.pdf?download=1',
  ]) assert.throws(() => p.cardURL(value), undefined, value);
});

test('ISO act dates validate calendar days including leap years', async () => {
  const source = await readFile(new URL('protocol.js', ROOT), 'utf8');
  const context = vm.createContext({URL, URLSearchParams});
  vm.runInContext(source, context);
  const p = context.SroKadProtocol;

  assert.equal(p.actDate('2024-02-29'), '2024-02-29');
  assert.equal(p.actDate('2000-02-29'), '2000-02-29');
  for (const value of ['2023-02-29', '1900-02-29', '2024-02-30', '2024-13-01', '2024-2-09', 20240229]) {
    assert.throws(() => p.actDate(value), /invalid_act_date/);
  }
});

test('date selection requires complete inventory and exactly one candidate on the requested date', async () => {
  const source = await readFile(new URL('protocol.js', ROOT), 'utf8');
  const context = vm.createContext({URL, URLSearchParams});
  vm.runInContext(source, context);
  const {automaticDocument} = context.SroKadProtocol;
  const candidates = [
    {url: PDF, actDate: '2024-02-29'},
    {url: OTHER_PDF, actDate: '2024-03-01'},
  ];

  const unique = automaticDocument(candidates, '2024-02-29');
  assert.equal(unique.candidate.url, candidates[0].url);
  assert.equal(unique.reason, 'unique_act_date');
  assert.equal(automaticDocument([
    ...candidates,
    {url: NON_UUID_PDF, actDate: '2024-02-29'},
  ], '2024-02-29').error, 'ambiguous_document_date');
  assert.equal(automaticDocument(candidates, '2024-02-28').error, 'document_date_not_found');
  assert.equal(automaticDocument([
    candidates[0], {url: OTHER_PDF},
  ], '2024-02-29').error, 'document_date_missing');
});

test('sender must be this extension, a top-level tab, and on the exact KAD origin', async () => {
  const h = await harness();
  const invalidSenders = [
    h.sender(1, { id: 'other-extension' }),
    h.sender(1, { tab: {} }),
    h.sender(1, { tab: { id: '1' } }),
    h.sender(1, { frameId: 2 }),
    h.sender(1, { url: 'https://kad.arbitr.ru.evil.test/' }),
    h.sender(1, { url: 'https://user@kad.arbitr.ru/' }),
    h.sender(1, { url: 'file:///tmp/kad.html' }),
  ];
  for (const from of invalidSenders) {
    const result = await h.send({ type: 'begin', requestId: RUN, caseNumber: CASE }, from);
    assert.equal(typeof result.error, 'string');
  }
  assert.equal(h.stored.size, 0);
});

test('valid job advances search → card → documents → selected candidate on the same card', async () => {
  const h = await harness();
  let result = await h.send({ type: 'begin', requestId: RUN, caseNumber: CASE });
  assert.equal(result.job.phase, 'searching');
  assert.equal(result.job.caseNumber, CASE);

  result = await h.send({ type: 'card', requestId: RUN, cardUrl: CARD });
  assert.equal(result.job.phase, 'card');
  assert.equal(result.job.cardUrl, CARD);

  result = await h.send({ type: 'candidates', requestId: RUN, candidates: [
    { url: PDF, label: 'Синтетический акт.pdf' },
    { url: OTHER_PDF, label: 'Синтетическое определение.pdf' },
  ] }, h.sender(1, { url: CARD }));
  assert.equal(result.job.phase, 'documents');
  assert.equal(result.job.candidates.length, 2);

  result = await h.send({ type: 'select', requestId: RUN, url: OTHER_PDF }, h.sender(1, { url: CARD }));
  assert.equal(result.job.selectedUrl, OTHER_PDF);
  assert.equal(result.job.selectionReason, 'employee_selected_visible_document');
});

test('missing, mismatched, and expired job messages fail closed', async () => {
  const h = await harness();
  assert.deepEqual((await h.send({ type: 'get' })).job, null);
  assert.equal(typeof (await h.send({ type: 'card', requestId: RUN, cardUrl: CARD })).error, 'string');

  await h.send({ type: 'begin', requestId: RUN, caseNumber: CASE });
  assert.equal(typeof (await h.send({ type: 'card', requestId: OTHER_CARD_ID, cardUrl: CARD })).error, 'string');

  h.stored.set('kad:1', { requestId: RUN, caseNumber: CASE, phase: 'searching', startedAt: Date.now() - 31 * 60 * 1000 });
  const expired = await h.send({ type: 'card', requestId: RUN, cardUrl: CARD });
  assert.equal(typeof expired.error, 'string');
  assert.equal(h.stored.has('kad:1'), false);
});

test('replaying the same begin request is idempotent and starts no replacement search', async () => {
  const h = await harness();
  const first = await h.send({ type: 'begin', requestId: RUN, caseNumber: CASE });
  const replay = await h.send({ type: 'begin', requestId: RUN, caseNumber: CASE });
  assert.equal(replay.replay, true);
  assert.equal(replay.job.startedAt, first.job.startedAt);
  assert.equal(h.stored.get('kad:1').startedAt, first.job.startedAt);
});

test('background preserves a bridge through prepared-to-begin and reports replay only after transition', async () => {
  const h = await harness();
  const service = {
    id: h.extensionApi.runtime.id,
    tab: {id: 44},
    frameId: 0,
    url: 'http://localhost:8000/',
  };
  const opened = await h.send({type: 'bridge-start', requestId: RUN, caseNumber: CASE}, service);
  assert.equal(opened.phase, 'searching');
  const kadTabId = h.stored.get('service:44').tabId;
  const kad = h.sender(kadTabId, {url: 'https://kad.arbitr.ru/'});

  const prepared = h.stored.get(`kad:${kadTabId}`);
  assert.equal(prepared.phase, 'prepared');
  const started = await h.send({type: 'begin', requestId: RUN, caseNumber: CASE}, kad);
  assert.equal(started.job.phase, 'searching');
  assert.deepEqual(started.job.bridge, prepared.bridge);
  assert.equal(started.replay, undefined);

  const replay = await h.send({type: 'begin', requestId: RUN, caseNumber: CASE}, kad);
  assert.equal(replay.replay, true);
  assert.equal(replay.job.phase, 'searching');
  assert.deepEqual(replay.job.bridge, prepared.bridge);
});

test('Firefox MV3 background scripts work without importScripts and with browser namespace only', async () => {
  const h = await harness({browserOnly: true, withImportScripts: false});
  assert.equal(h.context.chrome, undefined);
  const started = await h.send({type: 'begin', requestId: RUN, caseNumber: CASE});
  assert.equal(started.job.phase, 'searching');
  const card = await h.send({type: 'card', requestId: RUN, cardUrl: CARD});
  assert.equal(card.job.cardUrl, CARD);
  assert.equal(h.stored.get('kad:1').phase, 'card');
});

test('extension manifest declares compatible dual MV3 background formats and the Chrome debugger permission', async () => {
  const manifest = JSON.parse(await readFile(new URL('manifest.json', ROOT), 'utf8'));
  assert.equal(manifest.version, '0.2.10');
  assert.equal(manifest.minimum_chrome_version, '121');
  assert.equal(manifest.browser_specific_settings.gecko.strict_min_version, '128');
  assert.deepEqual(manifest.background.scripts, ['protocol.js', 'native-pdf.js', 'bridge.js', 'background.js']);
  assert.equal(manifest.background.service_worker, 'background.js');
  assert.deepEqual(manifest.permissions, ['storage', 'debugger']);
  assert.deepEqual(manifest.host_permissions, [
    'https://kad.arbitr.ru/*',
    'http://localhost/*',
    'http://127.0.0.1/*',
  ]);
});

test('popup uses the browser namespace to launch KAD without requiring chrome', async () => {
  const protocolSource = await readFile(new URL('protocol.js', ROOT), 'utf8');
  const popupSource = await readFile(new URL('popup.js', ROOT), 'utf8');
  let submit;
  let opened;
  let closed = false;
  let prevented = false;
  const elements = {
    '#search': {addEventListener(_type, listener) { submit = listener; }},
    '#case': {value: CASE},
    '#act-date': {value: ''},
    '#status': {textContent: ''},
  };
  const browser = {tabs: {async create({url}) { opened = url; }}};
  const context = vm.createContext({
    URL, URLSearchParams,
    browser,
    document: {querySelector(selector) { return elements[selector]; }},
    crypto: {randomUUID: () => RUN},
    window: {close() { closed = true; }},
  });
  vm.runInContext(protocolSource, context, {filename: 'protocol.js'});
  vm.runInContext(popupSource, context, {filename: 'popup.js'});
  await submit({preventDefault() { prevented = true; }});
  assert.equal(context.chrome, undefined);
  assert.equal(prevented, true);
  assert.equal(opened, `https://kad.arbitr.ru/#sro_robot=1&case=${encodeURIComponent(CASE)}&run=${RUN}`);
  assert.equal(closed, true);
});

test('Chrome popup checks the install-time debugger permission and does not request it', async () => {
  const popupSource = await readFile(new URL('popup.js', ROOT), 'utf8');
  const calls = [];
  const elements = {
    '#search': {addEventListener() {}},
    '#enable-pdf': {disabled: false, textContent: '', addEventListener() { throw new Error('debugger click request'); }},
    '#status': {textContent: ''},
  };
  const context = vm.createContext({
    navigator: {userAgent: 'Mozilla/5.0 Chrome/131.0.0.0 Safari/537.36'},
    chrome: {permissions: {
      async contains(value) { calls.push(['contains', value]); return true; },
      async request(value) { calls.push(['request', value]); return false; },
    }, runtime: {}},
    document: {querySelector(selector) { return elements[selector]; }},
  });
  vm.runInContext(popupSource, context, {filename: 'popup.js'});
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], 'contains');
  assert.equal(JSON.stringify(calls[0][1]), JSON.stringify({permissions: ['debugger']}));
  assert.equal(elements['#enable-pdf'].disabled, true);
  assert.equal(elements['#enable-pdf'].textContent, 'Передача PDF включена');
});

test('prepared bridge binds the requested date through launch, begin, and unique candidate selection', async () => {
  const h = await harness();
  const service = {
    id: h.extensionApi.runtime.id,
    tab: {id: 45},
    frameId: 0,
    url: 'http://localhost:8000/',
  };
  const requestedDate = '2024-02-29';
  const opened = await h.send({type: 'bridge-start', requestId: RUN, caseNumber: CASE, actDate: requestedDate}, service);
  assert.equal(opened.phase, 'searching');
  const binding = h.stored.get('service:45');
  const kadTab = h.tabs.get(binding.tabId);
  assert.match(kadTab.url, /act_date=2024-02-29/);
  const prepared = h.stored.get(`kad:${binding.tabId}`);
  assert.equal(prepared.actDate, requestedDate);

  const kad = h.sender(binding.tabId, {url: 'https://kad.arbitr.ru/'});
  const started = await h.send({type: 'begin', requestId: RUN, caseNumber: CASE, actDate: requestedDate}, kad);
  assert.equal(started.job.actDate, requestedDate);
  await h.send({type: 'card', requestId: RUN, cardUrl: CARD}, kad);
  const otherDate = '2024-03-01';
  const inventory = [
    {url: PDF, label: 'Synthetic matching act', actDate: requestedDate},
    {url: OTHER_PDF, label: 'Synthetic later event', actDate: otherDate},
  ];
  await h.send({type: 'candidates', requestId: RUN, candidates: inventory}, h.sender(binding.tabId, {url: CARD}));
  const wrong = await h.send({type: 'select', requestId: RUN, url: OTHER_PDF, automatic: true}, h.sender(binding.tabId, {url: CARD}));
  assert.equal(typeof wrong.error, 'string');
  const selected = await h.send({type: 'select', requestId: RUN, url: PDF, automatic: true}, h.sender(binding.tabId, {url: CARD}));
  assert.equal(selected.job.selectedUrl, PDF);
  assert.equal(selected.job.selectionReason, 'unique_act_date');
});

test('capture rejection persists a stopped job and returns only an allowlisted error', async () => {
  const secret = 'synthetic raw native transport details';
  const h = await harness({nativeCapture: async () => { throw new Error(secret); }});
  const serviceTab = 44;
  h.tabs.set(serviceTab, {id: serviceTab, url: 'http://localhost:8000/'});
  h.stored.set(`service:${serviceTab}`, {requestId: RUN, tabId: 1, origin: 'http://localhost:8000'});
  h.stored.set('kad:1', {
    requestId: RUN, caseNumber: CASE, phase: 'documents', startedAt: Date.now(),
    bridge: {serviceTab, origin: 'http://localhost:8000'}, cardUrl: CARD,
    candidates: [{url: PDF, label: 'Synthetic act'}], selectedUrl: PDF,
    selectionReason: 'only_visible_document',
  });

  const response = await h.send({type: 'acquire', requestId: RUN}, h.sender(1, {url: CARD}));
  const saved = h.stored.get('kad:1');
  assert.equal(response.code, 'helper_state');
  assert.equal(response.error, 'Потеряна связь с состоянием запуска помощника.');
  assert.doesNotMatch(response.error, /synthetic raw native transport details/);
  assert.equal(saved.phase, 'stopped');
  assert.equal(saved.reason, response.error);
});

test('valid stop cancels active capture while unrelated run or page cannot cancel it', async () => {
  let captureSignal;
  let beginCapture;
  const captureStarted = new Promise(resolve => { beginCapture = resolve; });
  const h = await harness({nativeCapture: ({signal}) => {
    captureSignal = signal;
    beginCapture();
    return new Promise(resolve => signal.addEventListener('abort', () => resolve(new TextEncoder().encode('%PDF-1.7 synthetic bytes')), {once: true}));
  }});
  const serviceTab = 44;
  h.tabs.set(serviceTab, {id: serviceTab, url: 'http://localhost:8000/'});
  h.stored.set(`service:${serviceTab}`, {requestId: RUN, tabId: 1, origin: 'http://localhost:8000'});
  h.stored.set('kad:1', {
    requestId: RUN, caseNumber: CASE, phase: 'documents', startedAt: Date.now(),
    bridge: {serviceTab, origin: 'http://localhost:8000'}, cardUrl: CARD,
    candidates: [{url: PDF, label: 'Synthetic act'}], selectedUrl: PDF,
    selectionReason: 'only_visible_document',
  });

  const acquiring = h.send({type: 'acquire', requestId: RUN}, h.sender(1, {url: CARD}));
  await captureStarted;
  const unrelatedRun = h.send({type: 'stop', requestId: OTHER_CARD_ID, reason: 'aborted'}, h.sender(1, {url: CARD}));
  assert.equal(captureSignal.aborted, false, 'a different run cannot cancel this capture');
  const unrelatedPage = h.send({type: 'stop', requestId: RUN, reason: 'aborted'}, h.sender(1, {url: `https://kad.arbitr.ru/Card/${OTHER_CARD_ID}`}));
  assert.equal(captureSignal.aborted, false, 'a different card page cannot cancel this capture');

  const stop = h.send({type: 'stop', requestId: RUN, reason: 'aborted'}, h.sender(1, {url: CARD}));
  assert.equal(captureSignal.aborted, true, 'a matching run and card cancels capture before tab-lock processing');
  const acquisitionResult = await acquiring;
  assert.equal(acquisitionResult.code, 'acquisition_cancelled');
  await Promise.all([unrelatedRun, unrelatedPage, stop]);
  const saved = h.stored.get('kad:1');
  assert.equal(saved.phase, 'stopped');
  const bridgeStatus = await h.send({type: 'bridge-status', requestId: RUN}, h.sender(serviceTab, {url: 'http://localhost:8000/'}));
  assert.equal(bridgeStatus.phase, 'stopped');
});

test('late stop after capture resolves but before ready persistence withholds the ready receipt', async () => {
  const h = await harness({nativeCapture: async () => new TextEncoder().encode('%PDF-1.7 synthetic bytes')});
  const serviceTab = 44;
  h.tabs.set(serviceTab, {id: serviceTab, url: 'http://localhost:8000/'});
  h.stored.set(`service:${serviceTab}`, {requestId: RUN, tabId: 1, origin: 'http://localhost:8000'});
  h.stored.set('kad:1', {
    requestId: RUN, caseNumber: CASE, phase: 'documents', startedAt: Date.now(),
    bridge: {serviceTab, origin: 'http://localhost:8000'}, cardUrl: CARD,
    candidates: [{url: PDF, label: 'Synthetic act'}], selectedUrl: PDF,
    selectionReason: 'only_visible_document',
  });
  const originalSet = h.extensionApi.storage.session.set.bind(h.extensionApi.storage.session);
  let releaseReadyWrite;
  let readyWriteStarted;
  const readyWrite = new Promise(resolve => { readyWriteStarted = resolve; });
  const releaseWrite = new Promise(resolve => { releaseReadyWrite = resolve; });
  h.extensionApi.storage.session.set = async values => {
    if (values['kad:1']?.phase === 'ready') {
      readyWriteStarted();
      await releaseWrite;
    }
    return originalSet(values);
  };

  const acquisition = h.send({type: 'acquire', requestId: RUN}, h.sender(1, {url: CARD}));
  await readyWrite;
  h.context.SroKadBridge.cancel(1, RUN, CARD);
  releaseReadyWrite();
  const result = await acquisition;
  assert.equal(result.job.phase, 'ready', 'the in-flight handler may finish, but aborted bridge reads must not expose bytes');
  const status = await h.send({type: 'bridge-status', requestId: RUN}, h.sender(serviceTab, {url: 'http://localhost:8000/'}));
  assert.equal(status.phase, 'stopped');
  assert.equal(status.body, undefined);
  const importing = await h.send({type: 'bridge-importing', requestId: RUN}, h.sender(serviceTab, {url: 'http://localhost:8000/'}));
  assert.equal(importing.code, 'aborted');
});

test('stop after bridge-importing admission cannot cancel the committed capture', async () => {
  let captureSignal;
  const h = await harness({nativeCapture: async ({signal}) => {
    captureSignal = signal;
    return new TextEncoder().encode('%PDF-1.7 synthetic bytes');
  }});
  const serviceTab = 44;
  h.tabs.set(1, {id: 1, url: CARD});
  h.tabs.set(serviceTab, {id: serviceTab, url: 'http://localhost:8000/'});
  h.stored.set(`service:${serviceTab}`, {requestId: RUN, tabId: 1, origin: 'http://localhost:8000'});
  h.stored.set('kad:1', {
    requestId: RUN, caseNumber: CASE, phase: 'documents', startedAt: Date.now(),
    bridge: {serviceTab, origin: 'http://localhost:8000'}, cardUrl: CARD,
    candidates: [{url: PDF, label: 'Synthetic act'}], selectedUrl: PDF,
    selectionReason: 'only_visible_document',
  });

  const acquisition = await h.send({type: 'acquire', requestId: RUN}, h.sender(1, {url: CARD}));
  assert.equal(acquisition.job.phase, 'ready');
  assert.equal((await h.send({type: 'bridge-importing', requestId: RUN}, h.sender(serviceTab, {url: 'http://localhost:8000/'}))).ok, true);
  const stop = await h.send({type: 'stop', requestId: RUN, reason: 'aborted'}, h.sender(1, {url: CARD}));
  assert.equal(stop.code, 'import_started');
  assert.equal(captureSignal.aborted, false);
  const status = await h.send({type: 'bridge-status', requestId: RUN}, h.sender(serviceTab, {url: 'http://localhost:8000/'}));
  assert.equal(status.phase, 'ready');
  assert.equal(typeof status.body, 'string');
});

test('jobs are isolated by tab and concurrent messages for one tab serialize', async () => {
  const h = await harness();
  const [a, b] = await Promise.all([
    h.send({ type: 'begin', requestId: RUN, caseNumber: CASE }, h.sender(1)),
    h.send({ type: 'begin', requestId: 'bbbbbbbb-cccc-dddd-eeee-ffffffffffff', caseNumber: 'А99-12345/2099' }, h.sender(2)),
  ]);
  assert.equal(a.job.requestId, RUN);
  assert.equal(b.job.requestId, 'bbbbbbbb-cccc-dddd-eeee-ffffffffffff');
  assert.notEqual(h.stored.get('kad:1'), h.stored.get('kad:2'));

  const [first, second] = await Promise.all([
    h.send({ type: 'begin', requestId: '11111111-aaaa-bbbb-cccc-222222222222', caseNumber: CASE }, h.sender(3)),
    h.send({ type: 'begin', requestId: '33333333-aaaa-bbbb-cccc-444444444444', caseNumber: CASE }, h.sender(3)),
  ]);
  assert.equal(first.job.requestId, '11111111-aaaa-bbbb-cccc-222222222222');
  assert.equal(second.job.requestId, '33333333-aaaa-bbbb-cccc-444444444444');
  assert.equal(h.stored.get('kad:3').requestId, second.job.requestId);
});

test('candidate list and selection must belong to the current card and candidate set', async () => {
  const h = await harness();
  await h.send({ type: 'begin', requestId: RUN, caseNumber: CASE });
  await h.send({ type: 'card', requestId: RUN, cardUrl: CARD });

  const wrongCard = await h.send({ type: 'candidates', requestId: RUN, candidates: [
    { url: OTHER_CARD_PDF, label: 'Wrong card.pdf' },
  ] }, h.sender(1, { url: CARD }));
  assert.equal(typeof wrongCard.error, 'string');

  const wrongPage = await h.send({ type: 'candidates', requestId: RUN, candidates: [
    { url: PDF, label: 'Valid.pdf' },
  ] }, h.sender(1, { url: `https://kad.arbitr.ru/Card/${OTHER_CARD_ID}` }));
  assert.equal(typeof wrongPage.error, 'string');

  await h.send({ type: 'candidates', requestId: RUN, candidates: [
    { url: PDF, label: 'Valid.pdf' },
  ] }, h.sender(1, { url: CARD }));
  const arbitrary = await h.send({ type: 'select', requestId: RUN, url: OTHER_PDF }, h.sender(1, { url: CARD }));
  assert.equal(typeof arbitrary.error, 'string');
});

test('candidate path must contain card UUID, document UUID, and PDF filename; duplicate URLs fail', async () => {
  const h = await harness();
  await h.send({ type: 'begin', requestId: RUN, caseNumber: CASE });
  await h.send({ type: 'card', requestId: RUN, cardUrl: CARD });
  for (const candidates of [
    [{ url: NON_UUID_PDF, label: 'Non-UUID path.pdf' }],
    [{ url: OTHER_CARD_PDF, label: 'Different card.pdf' }],
    [{ url: PDF, label: 'First.pdf' }, { url: PDF, label: 'Duplicate.pdf' }],
  ]) {
    const result = await h.send({ type: 'candidates', requestId: RUN, candidates }, h.sender(1, { url: CARD }));
    assert.equal(typeof result.error, 'string');
    assert.notEqual(h.stored.get('kad:1').phase, 'documents');
  }
});

test('background uses no direct network API and accepts no arbitrary URL for navigation or fetch', async () => {
  const [background, protocol, automation] = await Promise.all([
    readFile(new URL('background.js', ROOT), 'utf8'),
    readFile(new URL('protocol.js', ROOT), 'utf8'),
    readFile(new URL('automation.js', ROOT), 'utf8'),
  ]);
  for (const source of [background, protocol, automation]) {
    assert.doesNotMatch(source, /\bfetch\s*\(|\bXMLHttpRequest\b|\bWebSocket\b/);
  }
  assert.doesNotMatch(background, /chrome\.tabs\.(?:create|update)\s*\(/);
  assert.doesNotMatch(background, /chrome\.downloads\./);
});


test('automatic document selection requires one candidate and cannot repeat', async () => {
  for (const urls of [[PDF], [PDF, OTHER_PDF]]) {
    const h = await harness();
    await h.send({type: 'begin', requestId: RUN, caseNumber: CASE});
    await h.send({type: 'card', requestId: RUN, cardUrl: CARD});
    const sender = h.sender(1, {url: CARD});
    await h.send({type: 'candidates', requestId: RUN, candidates: urls.map(url => ({url, label: 'Synthetic act'}))}, sender);
    const result = await h.send({type: 'select', requestId: RUN, url: PDF, automatic: true}, sender);
    if (urls.length === 1) {
      assert.equal(result.job.selectionReason, 'only_visible_document');
      assert.ok((await h.send({type: 'select', requestId: RUN, url: PDF, automatic: true}, sender)).error);
    } else assert.ok(result.error);
  }
});


test('gateway reload is limited to the same search or card page and does not restart a submitted search', async () => {
  const h = await harness();
  await h.send({type: 'begin', requestId: RUN, caseNumber: CASE});
  assert.equal(h.stored.get('kad:1').gatewayReloads, 0);
  assert.ok((await h.send({type: 'gateway-resumed', requestId: RUN})).error);
  assert.ok((await h.send({type: 'gateway-reload', requestId: RUN}, h.sender(1, {url: 'https://kad.arbitr.ru/?q=1'}))).error);
  assert.ok((await h.send({type: 'gateway-reload', requestId: RUN}, h.sender(1, {url: CARD}))).error);

  const first = await h.send({type: 'gateway-reload', requestId: RUN});
  assert.equal(first.delayMs, 2000);
  assert.equal(first.reloadLimit, 2);
  assert.equal(first.job.gatewayReloads, 1);
  assert.equal(first.job.gatewayResume, true);
  const resumed = await h.send({type: 'gateway-resumed', requestId: RUN});
  assert.equal(resumed.job.gatewayResume, false);
  assert.equal(resumed.job.gatewayReloads, 1);
  const second = await h.send({type: 'gateway-reload', requestId: RUN});
  assert.equal(second.delayMs, 6000);
  assert.equal(second.job.gatewayReloads, 2);
  const third = await h.send({type: 'gateway-reload', requestId: RUN});
  assert.equal(third.code, 'gateway-unavailable');
  assert.equal(h.stored.get('kad:1').phase, 'searching');
  assert.equal(h.stored.get('kad:1').gatewayReloads, 2);

  await h.send({type: 'gateway-resumed', requestId: RUN});
  const card = await h.send({type: 'card', requestId: RUN, cardUrl: CARD});
  assert.equal(card.job.gatewayReloads, 0);
  assert.equal(card.job.gatewayResume, false);
  const cardReload = await h.send({type: 'gateway-reload', requestId: RUN}, h.sender(1, {url: CARD}));
  assert.equal(cardReload.delayMs, 2000);
  assert.equal(cardReload.job.gatewayReloads, 1);
  assert.ok((await h.send({type: 'gateway-resumed', requestId: RUN})).error);
  const stopped = await h.send({type: 'stop', requestId: RUN, reason: 'gateway-after-submit'}, h.sender(1, {url: CARD}));
  assert.match(stopped.job.reason, /после команды «Найти»/);
  assert.ok((await h.send({type: 'gateway-reload', requestId: RUN}, h.sender(1, {url: CARD}))).error);
});

test('short background ticks require the active run and exact page and preserve state', async () => {
  const h = await harness({browserOnly: true, withImportScripts: false});
  assert.ok((await h.send({type: 'tick', requestId: RUN})).error);
  await h.send({type: 'begin', requestId: RUN, caseNumber: CASE});
  const before = h.stored.get('kad:1');
  assert.equal((await h.send({type: 'tick', requestId: RUN})).ok, true);
  assert.equal(h.stored.get('kad:1'), before);
  assert.ok((await h.send({type: 'tick', requestId: OTHER_CARD_ID})).error);
  assert.ok((await h.send({type: 'tick', requestId: RUN}, h.sender(1, {url: CARD}))).error);
  await h.send({type: 'card', requestId: RUN, cardUrl: CARD});
  assert.equal((await h.send({type: 'tick', requestId: RUN}, h.sender(1, {url: CARD}))).ok, true);
  assert.ok((await h.send({type: 'tick', requestId: RUN})).error);
  const stopped = await h.send({type: 'stop', requestId: RUN, reason: 'search-filter'});
  assert.match(stopped.job.reason, /Кнопка «Найти» не нажималась/);
  assert.ok((await h.send({type: 'tick', requestId: RUN}, h.sender(1, {url: CARD}))).error);
});
